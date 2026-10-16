# Changelog

## 0.1.0rc5 - 2026-10-16

- Bind automatic transfer sets to the resolved shapefile folder as well as the
  network fingerprint, so moving an unchanged network is a first-time run in
  its new location.
- Reject automatic dictionary paths that resolve outside the selected
  shapefile folder's local `NVTA_Attribute_Transfer` directory.

## 0.1.0rc4 - 2026-10-16

- Automatically prepare and apply fingerprint-matched future-network transfer
  dictionaries whenever network conversion runs.
- Keep immutable dictionary sets beside the source shapefile under
  `NVTA_Attribute_Transfer`, retaining earlier sets for debugging.
- Create a new set after any source-network edit and prevent assignment from
  accidentally consuming stale manually selected dictionaries.
- Bundle the compact 2025 geometry and accepted corridor references required
  for client-side transfers without external calibration inputs.

## 0.1.0rc3 - 2026-10-16

- Transfer base-year AM/MD/PM QVDF parameters to arbitrary future-year
  networks with auditable exact-pair, replacement-path, geometry, corridor,
  and link-type fallback provenance.
- Generate separate scenario-local expanded QVDF and speed/T dictionaries;
  packaged base-year resources remain read-only and hash-verified.
- Carry speed anchors and complete T0/T2/T3 triplets only through exact,
  replacement-path, and accepted high-confidence geometry lineage. Corridor
  and link-type fallback links intentionally remain blank.
- Allow assignment to consume the generated speed/T dictionary with
  `--speed-t-override-dictionary` while retaining normal NT behavior.

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
