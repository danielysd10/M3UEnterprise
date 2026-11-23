from pathlib import Path
import ast
import re


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def jobs():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    blocks = re.split(r"^  ([\w-]+):\s*$", workflow.split("\njobs:\n", 1)[1], flags=re.MULTILINE)
    return dict(zip(blocks[1::2], blocks[2::2]))


def test_ci_uses_package_outputs_and_attests_release_and_sbom():
    workflow_jobs = jobs()
    verify = workflow_jobs["verify"]
    assert "id: package" in verify
    verify_pair = 'tools/verify_release.py --zip "${{ steps.package.outputs.release_zip }}" --sbom "${{ steps.package.outputs.release_sbom }}"'
    artifact = "m3u-enterprise-verified-release-python-${{ matrix.python-version }}"
    assert verify.index(verify_pair) < verify.index("uses: actions/upload-artifact@") < verify.index(artifact)
    upload = verify.split("uses: actions/upload-artifact@", 1)[1].split("\n      - ", 1)[0]
    assert "steps.package.outputs.release_zip" in upload and "steps.package.outputs.release_sbom" in upload
    assert "M3U_ENTERPRISE_PRODUCTION_READY_OPTIMIZED_V9_0_" not in verify
    assert "contents: read" in verify and ": write" not in verify
    assert "actions/attest@" not in verify, "The job that runs product code must not sign it."

    attest = workflow_jobs["attest"]
    assert "needs: verify" in attest
    guard = re.search(r"^    if: (.*)$", attest, re.MULTILINE).group(1)
    assert guard == "(github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref_protected"
    assert "attestations: write" in attest and "id-token: write" in attest
    assert "actions/checkout@" not in attest
    steps = re.split(r"^      - ", attest, flags=re.MULTILINE)[1:]
    download, = [step for step in steps if "uses: actions/download-artifact@" in step]
    assert artifact in download
    assert "digest-mismatch: error" in download
    assert not re.search(r"^\s*(run-id|repository|github-token):", download, re.MULTILINE)

    claims = [step for step in steps if "uses: actions/attest@" in step]
    assert len(claims) == 2, "Emit separate SLSA provenance and CycloneDX statements."
    provenance, = [step for step in claims if "sbom-path:" not in step]
    sbom, = [step for step in claims if "sbom-path:" in step]
    for step in claims:
        assert steps.index(download) < steps.index(step)
        assert re.search(r"""^\s*subject-path: ["']?\${{ steps\.pair\.outputs\.zip }}["']?$""", step, re.MULTILINE)
    assert re.search(r"""^\s*sbom-path: ["']?\${{ steps\.pair\.outputs\.sbom }}["']?$""", sbom, re.MULTILINE)
    assert not re.search(r"^\s*predicate(?:-type|-path)?:", provenance, re.MULTILINE)

    check, = [step for step in steps if "gh attestation verify" in step]
    assert max(steps.index(step) for step in claims) < steps.index(check)
    for step in (provenance, sbom):
        step_id = re.search(r"^\s*id: ([\w-]+)", step, re.MULTILINE).group(1)
        assert f"steps.{step_id}.outputs.bundle-path" in check
    for binding in ("'--repo', $env:GITHUB_REPOSITORY",
                    "'--signer-workflow', \"$env:GITHUB_REPOSITORY/.github/workflows/ci.yml\"",
                    "'--source-digest', $env:GITHUB_SHA", "'--source-ref', $env:GITHUB_REF"):
        assert binding in check
    calls = [line for line in check.splitlines() if "gh attestation verify" in line]
    assert len(calls) == 2 and all("gh attestation verify $env:RELEASE_ZIP @identity" in line for line in calls)
    assert "--bundle $env:PROVENANCE_BUNDLE --predicate-type https://slsa.dev/provenance/v1" in calls[0]
    assert "--bundle $env:SBOM_BUNDLE --predicate-type https://cyclonedx.org/bom --format json" in calls[1]
    assert "['verificationResult']['statement']['predicate']" in check
    assert "ReadAllText($env:RELEASE_SBOM)" in check and "DeepEquals($expected, $predicate)" in check

    for job in workflow_jobs.values():
        for ref in re.findall(r"^\s*(?:- )?uses: (\S+)", job, re.MULTILINE):
            assert re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", ref), ref


def test_ci_verifies_each_supported_python_with_the_locked_toolchain():
    workflow_jobs = jobs()
    workflow = workflow_jobs["verify"]
    matrix_pattern = r"^\s+python-version: (\[[^\n]+\])$"
    versions = ast.literal_eval(re.search(matrix_pattern, workflow, re.MULTILINE).group(1))
    signing_versions = ast.literal_eval(re.search(matrix_pattern, workflow_jobs["attest"], re.MULTILINE).group(1))
    assert versions == signing_versions, "The signing matrix must consume every verified ZIP."
    assert len(versions) == 2
    assert all(re.fullmatch(r"3\.(13|14)\.\d+", version) for version in versions)
    assert {version.rsplit(".", 1)[0] for version in versions} == {"3.13", "3.14"}
    assert "python-version: ${{ matrix.python-version }}" in workflow
    assert "fail-fast: false" in workflow
    assert "python -m venv" in workflow and "$env:GITHUB_PATH" in workflow
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1'" in workflow
    run_blocks = re.findall(r"^\s{8}run: (.*(?:\n\s{10}[^\n]+)*)", workflow, re.MULTILINE)
    install_commands = [" ".join(block.split()) for block in run_blocks if "pip" in block and " install" in block]
    assert install_commands
    for command in install_commands:
        assert "--require-hashes" in command and "--only-binary=:all:" in command
        assert "-r requirements-dev.lock" in command
    for variable in ("M3U_CONFIG_DIR", "M3U_STATE_DIR"):
        line = next(line for line in workflow.splitlines() if variable + "=" in line)
        assert "$env:RUNNER_TEMP" in line and "matrix.python-version" in line
        assert "$env:GITHUB_ENV" in line
    job_env = workflow.split("    env:", 1)[1].split("    permissions:", 1)[0]
    assert "runner." not in job_env, "Runner context is unavailable in job-level env"
    artifact_names = re.findall(r"^\s+name: (m3u-enterprise-[^\n]+)$", workflow, re.MULTILINE)
    assert artifact_names and all("matrix.python-version" in name for name in artifact_names)
    assert 'SOURCE_DATE_EPOCH: \'315532800\'' in workflow
