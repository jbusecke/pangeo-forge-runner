import json
import re
import subprocess
import tempfile

import pytest
import xarray as xr

from pangeo_forge_runner.commands.bake import Bake


@pytest.fixture
def recipes_version_ref():
    # FIXME: recipes version matrix is currently determined by github workflows matrix
    # in the future, it should be set by pangeo-forge-runner venv feature?
    pip_list = subprocess.check_output("pip list".split()).decode("utf-8").splitlines()
    recipes_version = [
        p.split()[-1] for p in pip_list if p.startswith("pangeo-forge-recipes")
    ][0]
    return (
        "0.9.x"
        # FIXME: for now, beam-refactor is unreleased, so installing from the dev branch
        # gives something like "0.9.1.dev86+g6e9c341" as the version. So we just assume any
        # version which includes "dev" is the "beam-refactor" branch, because we're not
        # installing from any other upstream dev branch at this point. After beam-refactor
        # release, we can figure this out based on an explicit version tag, i.e. "0.10.*".
        if "dev" not in recipes_version
        else "beam-refactor"
    )


@pytest.mark.parametrize(
    "job_name, raises",
    (
        ["valid-job", False],
        ["valid_job", False],
        ["".join(["a" for i in range(63)]), False],  # <= 63 chars allowed
        ["".join(["a" for i in range(64)]), True],  # > 63 chars not allowed
        ["invali/d", True],  # dashes are the only allowable punctuation
        ["1valid-job", True],  # can only start with letters
        ["-valid-job", True],  # can only start with letters
        ["Valid-Job", True],  # uppercase letters not allowed
    ),
)
def test_job_name_validation(job_name, raises):
    bake = Bake()
    if raises:
        with pytest.raises(
            ValueError,
            match=re.escape(
                f"job_name must match the regex ^[a-z][-_0-9a-z]{{0,62}}$, instead found {job_name}"
            ),
        ):
            bake.job_name = job_name
    else:
        bake.job_name = job_name
        assert bake.job_name == job_name


@pytest.mark.parametrize(
    ("recipe_id", "expected_error", "custom_job_name"),
    (
        [None, None, None],
        ["gpcp-from-gcs", None, None],
        [
            "invalid_recipe_id",
            "ValueError: self.recipe_id='invalid_recipe_id' not in ['gpcp-from-gcs']",
            None,
        ],
        [None, None, "special-name-for-job"],
    ),
)
def test_gpcp_bake(
    minio, recipe_id, expected_error, custom_job_name, recipes_version_ref
):
    fsspec_args = {
        "key": minio["username"],
        "secret": minio["password"],
        "client_kwargs": {"endpoint_url": minio["endpoint"]},
    }

    config = {
        "Bake": {
            "prune": True,
            "bakery_class": "pangeo_forge_runner.bakery.local.LocalDirectBakery",
        },
        "TargetStorage": {
            "fsspec_class": "s3fs.S3FileSystem",
            "fsspec_args": fsspec_args,
            "root_path": "s3://gpcp/target/",
        },
        "InputCacheStorage": {
            "fsspec_class": "s3fs.S3FileSystem",
            "fsspec_args": fsspec_args,
            "root_path": "s3://gpcp/input-cache/",
        },
        "MetadataCacheStorage": {
            "fsspec_class": "s3fs.S3FileSystem",
            "fsspec_args": fsspec_args,
            "root_path": "s3://gpcp/metadata-cache/",
        },
    }

    if recipe_id:
        config["Bake"].update({"recipe_id": recipe_id})
    if custom_job_name:
        config["Bake"].update({"job_name": custom_job_name})

    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        json.dump(config, f)
        f.flush()
        cmd = [
            "pangeo-forge-runner",
            "bake",
            "--repo",
            "https://github.com/pforgetest/gpcp-from-gcs-feedstock.git",
            "--ref",
            # in the test feedstock, tags are named for the recipes version
            # which was used to write the recipe module
            recipes_version_ref,
            "--json",
            "-f",
            f.name,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        stdout = proc.stdout.decode().splitlines()

        if expected_error:
            assert proc.returncode == 1
            stdout[-1] == expected_error

        else:
            assert proc.returncode == 0

            for line in stdout:
                if "Running job for recipe gpcp" in line:
                    job_name = json.loads(line)["job_name"]

            if custom_job_name:
                assert job_name == custom_job_name
            else:
                assert job_name.startswith("gh-pforgetest-gpcp-from-gcs-")

            # Open the generated dataset with xarray!
            gpcp = xr.open_dataset(
                config["TargetStorage"]["root_path"],
                backend_kwargs={"storage_options": fsspec_args},
                engine="zarr",
            )

            assert (
                gpcp.title
                == "Global Precipitation Climatatology Project (GPCP) Climate Data Record (CDR), Daily V1.3"
            )
            # --prune prunes to two time steps by default, so we expect 2 items here
            assert len(gpcp.precip) == 2
            print(gpcp)

            # `mc` isn't the best way, but we want to display all the files in our minio
            with tempfile.TemporaryDirectory() as mcd:
                cmd = [
                    "mc",
                    "--config-dir",
                    mcd,
                    "alias",
                    "set",
                    "local",
                    minio["endpoint"],
                    minio["username"],
                    minio["password"],
                ]

                subprocess.run(cmd, check=True)

                cmd = ["mc", "--config-dir", mcd, "ls", "--recursive", "local"]
                subprocess.run(cmd, check=True)