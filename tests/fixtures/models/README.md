# Large fixtures — provenance

Not committed. 47 MB of public data belongs in a download; what belongs in git is
where it came from. Fetch with `scripts/fetch_fixtures.sh`.

## schependomlaan_design.ifc

- **Source:** `openBIMstandards/Archive-DataSetSchependomlaan`, path
  `Design model IFC/IFC Schependomlaan.ifc`
- **Why the archive:** the live `openBIMstandards/DataSetSchependomlaan` repo is
  now a README pointing at a location that returns 404.
- **Shape:** IFC2X3, 47 MB, 1,437 meshed elements — 73 MEP, 1,364 structural,
  6 storeys.
- **sha256:** `2c3565ca1904f2aa61adab92024cf3755b2c5b21a498144d3094d7cb58cebec7`
- **Known limits:** carries no `IfcDistributionPort`, so connectivity is
  unprovable on it; contains no gas main, so every seed clearance rule is
  unreachable against it (RUNLOG F1).

The companion utilities model `HB_Nutsvoorzieningen.ifc` is deliberately not used:
it contains zero MEP elements.

## Infra-Plumbing.ifc

- **Source:** `buildingSMART/Sample-Test-Files`, `IFC 4.0/Infra/`
- **Shape:** IFC4, 24 pipe segments, single discipline.
- **Use:** the second benchmark row. Single-discipline, so cross-system clash is
  impossible in it by construction — it measures the joint filter, not the judge.
