#!/usr/bin/env bash
set -e

echo "Running ruff linting&formatting"
ruff check --fix kirk
ruff format  kirk
echo "Running mypy ..."
mypy kirk
echo "Running pytest ..."
pytest .