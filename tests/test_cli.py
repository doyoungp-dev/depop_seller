"""The CLI must import and parse: every launcher (.cmd) goes through it."""

from __future__ import annotations

import subprocess
import sys

import pytest

from depop_seller.cli import build_parser


@pytest.mark.parametrize("argv", [["check"], ["sort", "20990101"], ["review"], ["sell"], ["hub"], ["claude-login"], ["learn"],
                                  ["hub", "--no-browser", "--wait-for-port", "30"], ["release"], ["release", "1.2.0", "--dry-run"]])
def test_every_subcommand_parses(argv):
    args = build_parser().parse_args(argv)
    assert callable(args.func)


def test_module_runs_as_a_program():
    out = subprocess.run([sys.executable, "-m", "depop_seller", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "sort" in out.stdout and "sell" in out.stdout
