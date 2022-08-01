from traitlets.config import Application
from traitlets import Unicode, List, Bool
from repo2docker import contentproviders
import sys
import logging
from pythonjsonlogger import jsonlogger


# Common aliases we want to support in *all* commands
# The key is what the commandline argument should be, and the
# value is the traitlet config it will be translated to
common_aliases = {
    'log-level': 'Application.log_level',
    'f': 'BaseCommand.config_file',
    'config': 'BaseCommand.config_file',
    'repo': 'BaseCommand.repo',
    'ref': 'BaseCommand.ref',
}

# Common flags we want to support in *all* commands.
# The key is the name of the flag, and the value is a tuple
# consisting of a dicitonary with the traitlet config that will be
# set, and a helpful description to be printed in the commandline
common_flags = {
    'json': (
        {'BaseCommand': {'json_logs': True}},
        "Generate JSON output"
    )
}

class BaseCommand(Application):
    """
    Base Application for all our subcommands.

    Provides common traitlets everyone needs, and base methods for
    fetching a given repository.

    Do not directly instantiate!
    """
    log_level = logging.INFO

    repo = Unicode(
        "",
        config=True,
        help="""
        URL of feedstock repo to operate on.

        Can be anything that is interpretable by self.content_providers,
        using Repo2Docker ContentProviders. By default, this includes Git
        repos, Mercurial Repos, Zenodo, Figshare, Dataverse, Hydroshare,
        Swhid and local file paths.
        """
    )

    ref = Unicode(
        None,
        allow_none=True,
        config=True,
        help="""
        Ref of feedstock repo to fetch.

        Optional, only used for some methods of fetching (such as git or
        mercurial)
        """
    )

    config_file = Unicode(
        'pangeo_forge_runner_config.py',
        config=True,
        help="""
        Load traitlet config from this file if it exists
        """
    )

    # Content providers from repo2docker are *solely* used to check out a repo
    # and get their contents locally, so we can work on them.
    content_providers = List(
        None,
        [
            contentproviders.Local,
            contentproviders.Zenodo,
            contentproviders.Figshare,
            contentproviders.Dataverse,
            contentproviders.Hydroshare,
            contentproviders.Swhid,
            contentproviders.Mercurial,
            contentproviders.Git,
        ],
        config=True,
        help="""
        List of ContentProviders to use to fetch repo.

        Uses ContentProviders from repo2docker for doing most of the work.
        The ordering matters, and Git is used as the default for any URL
        that we can not otherwise determine.

        If we want to support additional contentproviders, ideally we can
        contribute them upstream to repo2docker.
        """
    )

    json_logs = Bool(
        False,
        config=True,
        help="""
        Provide JSON formatted logging output to stdout.

        If set to True, *all* output will be emitted as one JSON object per
        line.

        Each line *will* have at least a 'status' field and a 'message' field.
        Various other keys will also be present based on the command being called
        and the value of 'status'.

        TODO: This *must* get a JSON schema.
        """
    )


    def fetch(self, target_path):
        """
        Fetch repo from url at ref, and check it out to checkout_path

        Uses repo2docker to detect what kinda url is going to be checked out,
        and fetches it into checkout_path. No image building or anything is
        performed.

        checkout_path should be empty.
        """
        picked_content_provider = None
        for ContentProvider in self.content_providers:
            cp = ContentProvider()
            spec = cp.detect(self.repo, ref=self.ref)
            if spec is not None:
                picked_content_provider = cp
                self.log.info(
                    "Picked {cp} content " "provider.\n".format(cp=cp.__class__.__name__),
                    extra={'status': 'fetching'}
                )
                break

        if picked_content_provider is None:
            raise ValueError(f'Could not fetch {self.repo}')

        for log_line in picked_content_provider.fetch(
            spec, target_path, yield_output=True
        ):
            self.log.info(log_line, extra=dict(status="fetching"))

    def json_excepthook(self, etype, evalue, traceback):
        """
        Called on an uncaught exception when using json logging

        Avoids non-JSON output on errors when using --json-logs
        """
        self.log.error(
            "Error during running: %s",
            evalue,
            exc_info=(etype, evalue, traceback),
            extra=dict(status="failed"),
        )

    def initialize(self, argv=None):
        super().initialize(argv)
        # Load traitlets config from a config file if present
        self.load_config_file(self.config_file)

        # The application communicates with the outside world via
        # stdout, and we structure this communication via logging.
        # So let's setup the default logger to log to stdout, rather
        # than stderr.
        logHandler = logging.StreamHandler(sys.stdout)
        self.log = logging.getLogger("pangeo-forge-runner")

        # Remove all existing handlers so we don't repeat messages
        self.log.handlers = []
        self.log.addHandler(logHandler)
        self.log.setLevel(self.log_level)

        if self.json_logs:
            # register JSON excepthook to avoid non-JSON output on uncaught exception
            sys.excepthook = self.json_excepthook
            formatter = jsonlogger.JsonFormatter()
            logHandler.setFormatter(formatter)
        else:
            # Since we also have JSON logging, we put newlines in our
            # messages wherever explicitly needed. Avoid the logger
            # adding its own, so we don't have unnecessary blank lines
            logHandler.terminator = ""
            # Just put out the message here, nothing else.
            logHandler.formatter = logging.Formatter(fmt="%(message)s")