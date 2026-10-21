# Changelog

## Unreleased

- Add `cad_chamfer` for native AutoCAD CHAMFER with distance/distance-angle methods, trim/no-trim, LINE/RAY/XLINE pairs, adjacent straight segments of one 2D polyline, whole-polyline and sequential batch operations. Default to zero-write preview; verify native geometry, restore settings/view, and roll back the entire batch on failure. Leave the DXF backend unchanged.

- Default MCP geometry creation to strict semantic identity: persist engineering object registrations inside drawings, require object/role bindings before direct drawing, and atomically create, attach and verify entity identity. Cover copy/array/block/layout and environment-model workflows through both toolsets; expose semantic reads and read-only audits. Add operator-only startup compatibility policy and block further writes after an unverified semantic rollback.

- Extend entity property editing to planar ARC, LWPOLYLINE, INSERT, HATCH, ELLIPSE and POINT. Preserve elevations/Z scale, per-segment polyline data, hatch loops/holes/associativity, and attached attribute geometry. Add formatted attribute values and copy-on-write fixed/nested block text edits scoped to the selected instance; preflight unsupported dependencies and verify rollback removes cloned definitions.

- Add `cad_get_entity_properties` and `cad_edit_entity_properties` for model-space entities in DXF and AutoCAD. Preview common appearance, TEXT/MTEXT content/height/rotation, LINE endpoints and CIRCLE center/radius; preflight resources and locked layers, verify readback and roll back failures without saving.

- Attach only to an already running AutoCAD instance; do not start or quit AutoCAD implicitly.
- Bound AutoCAD scans and busy retries, stop on interrupted reads, and preserve validation error details through gateway calls.
- Ignore floating-point endpoint noise and display real small gaps without rounding them to zero; do not modify geometry during checks.
- Execute stdio tools serially in a persistent worker monitored by the parent process, preserving existing tool schemas and state between calls. Bound startup, execution, queue waits, and blocked input pipes; terminate only the owned worker on failure and never replay failed requests.
- Mark unacknowledged writes as `EXECUTION_UNKNOWN`, prevent further writes/save/close/quit/mode changes, and require manual CAD inspection followed by an MCP restart. Reconnection does not establish rollback or remove the restriction; worker loss invalidates bindings, caches, tokens, and unsaved DXF memory state.
- Add isolated fault simulations, full/gateway stdio coverage, and an opt-in roundtrip against an already running AutoCAD; document cached health diagnostics and recovery limits.

- Default unconfigured sessions to AutoCAD/DWG (`acad`); preserve explicit DXF selection and configuration override precedence.

- Flatten the single-plugin repository: package metadata, source, tests, and skills now live at the repository root.
- Collect test and manual verification outputs under `.cache/` and align marketplace paths and versions.

- Consolidate user documentation in the repository root and `docs` directory for GitHub distribution.
- Document the observed Codex desktop AutoCAD cold-start crash and the manual-start workaround.
- Record that the same issue was not reproduced with `cc-haha`, without treating that observation as a compatibility guarantee.
- Remove the standalone ZIP release workflow and validate Python wheel builds in a temporary directory.

## 1.5.2

- Add `annotative` to `cad_make_block`, defaulting to `false` for existing calls.
- Create annotative blocks through AutoCAD's native `-BLOCK` command and verify the block definition, entity count, and source removal before reporting success. DXF mode rejects `annotative=true` without writing.

## 1.5.1

- Allow `cad_open_drawing` to open an existing local drive-letter absolute path outside the configured drawings directory, while preserving bare-filename compatibility and restricting new/save-as destinations.
- Reject missing files, wrong extensions, UNC paths, and path backtracking before attempting an AutoCAD connection; document explicit save-back behavior.

## 1.5.0

- Add `cad_audit_block_definition` for manifest-driven, read-only DWG block audits with recursive entity trees, geometry analysis, three-state comparisons, and versioned per-block fingerprints.
- Add `cad_fill_closed_boundary_solid` for DXF/AutoCAD SOLID hatches with dry-run, closed-boundary validation, immediate readback, and failure cleanup.
- Add strict `*_manifest.json` block specification cards; missing evidence remains `无法验证` and is never replaced by assumed defaults.

## 1.4.3

- Package the MCP server, Skill, and marketplace metadata in one repository.
- Move Python modules into a structured `src/cad_mcp` package.
- Add per-user JSON configuration and `cad-mcp config` commands.
- Keep MCP startup independent of the repository location.
