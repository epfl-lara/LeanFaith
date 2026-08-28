from __future__ import annotations

from typer.testing import CliRunner

from leanfaith.cli.app import app


def test_import_annotation_cli_is_registered_and_explicitly_non_adjudicating() -> None:
    result = CliRunner().invoke(app, ["import-annotation", "--help"])
    assert result.exit_code == 0
    assert "without adjudicating or promoting" in result.stdout
    assert "--bundle-manifest" in result.stdout
    assert "--private-linkage-manifest" in result.stdout
    assert "--human-assignment" in result.stdout
    assert "--human-submission-attestati" in result.stdout
    assert "--authentication-key" in result.stdout
    assert "--responses" in result.stdout


def test_authenticated_annotation_operator_commands_are_registered_and_fail_closed() -> None:
    runner = CliRunner()
    assignment = runner.invoke(app, ["create-human-assignment", "--help"])
    assert assignment.exit_code == 0
    assert "before the human can create responses" in assignment.stdout
    assert "--annotator-principal-hash" in assignment.stdout
    assert "--assigned-at" in assignment.stdout

    attestation = runner.invoke(app, ["attest-human-submission", "--help"])
    assert attestation.exit_code == 0
    assert "create no semantic label" in attestation.stdout
    assert "--confirm-operator-assertion" in attestation.stdout
    assert "--confirm-backend-export" in attestation.stdout

    agreement = runner.invoke(app, ["write-annotation-agreement", "--help"])
    assert agreement.exit_code == 0
    assert "write agreement statistics" in agreement.stdout
    assert "--first-import-manifest" in agreement.stdout
    assert "--second-import-manifest" in agreement.stdout

    queue = runner.invoke(app, ["write-adjudication-queue", "--help"])
    assert queue.exit_code == 0
    assert "never adjudicate automatically" in queue.stdout
    assert "--policy-trigger-set" in queue.stdout
