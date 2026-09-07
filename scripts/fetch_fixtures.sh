#!/usr/bin/env bash
# Fetch the large public models the acceptance run needs. Deliberately NOT
# committed: 47 MB of public data belongs in a download, and its provenance
# belongs in tests/fixtures/models/README.md.
set -euo pipefail
cd "$(dirname "$0")/../tests/fixtures/models"

# The ARCHIVED repo. The live openBIMstandards/DataSetSchependomlaan is now a
# README pointing at a location that 404s, so the archive is the working source.
BASE="https://raw.githubusercontent.com/openBIMstandards/Archive-DataSetSchependomlaan/master"
curl -fL -o schependomlaan_design.ifc "$BASE/Design%20model%20IFC/IFC%20Schependomlaan.ifc"

# buildingSMART Sample-Test-Files, used as the second benchmark model.
BS="https://raw.githubusercontent.com/buildingSMART/Sample-Test-Files/master"
curl -fL -o Infra-Plumbing.ifc "$BS/IFC%204.0/Infra/Infra-Plumbing.ifc" || \
  echo "Infra-Plumbing.ifc not fetched; the second benchmark row will be skipped"

echo "fetched:"
ls -lh *.ifc
