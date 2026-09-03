# Changelog

## 0.1.0rc2 - 2026-09-03

- Preserve true-shape link geometry through network conversion.
- Include street names in postprocessed link-performance output.
- Generate both NVTA jurisdiction-scoped and regional link-performance and
  statistics files.
- Fall back to the packaged jurisdiction lookup when it is absent from the
  scenario input.
- Write statistics CSV files with a single clean header row.

## 0.1.0rc1 - 2026-08-24

- Package the complete NVTA TAPLite workflow and its calibrated resources.
- Provide stable assignment, postprocessing, diagnostic, and version commands.
- Depend on the verified `taplite4mpo-pre-release==0.4.0rc1` engine.
- Include the August 23 stable-OD QVDF node-pair override dictionary.
- Run QVDF smoothing after assignment and before link-performance aggregation.
- Use local, automatically cleaned temporary storage for the smoother database
  when assignment output is located on a UNC network share.
