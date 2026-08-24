# Changelog

## 0.1.0rc1 - 2026-08-24

- Package the complete NVTA TAPLite workflow and its calibrated resources.
- Provide stable assignment, postprocessing, diagnostic, and version commands.
- Depend on the verified `taplite4mpo-pre-release==0.4.0rc1` engine.
- Include the August 23 stable-OD QVDF node-pair override dictionary.
- Run QVDF smoothing after assignment and before link-performance aggregation.
- Use local, automatically cleaned temporary storage for the smoother database
  when assignment output is located on a UNC network share.

