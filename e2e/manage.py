#!/usr/bin/env python
"""manage.py for the e2e host project (stapel-moderation) (run from the repo root)."""
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "e2e.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
