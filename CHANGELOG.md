# Changelog

## Unreleased

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
