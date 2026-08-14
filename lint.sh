#!/usr/bin/env bash
set -e

echo "Running ruff linting&formatting"
ruff check --fix .
ruff format .
echo "Running mypy ..."
mypy .
echo "Running pytest ..."
pytest .