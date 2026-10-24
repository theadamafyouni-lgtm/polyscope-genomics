#!/usr/bin/env python3
"""Invoke a disposable copy of the actual gate, without SSH or any remote host."""
import os
import subprocess
import sys

result = subprocess.run(['bash', '-p', sys.argv[1]],
                        env=dict(os.environ, SSH_ORIGINAL_COMMAND=sys.argv[2]))
sys.exit(result.returncode)
