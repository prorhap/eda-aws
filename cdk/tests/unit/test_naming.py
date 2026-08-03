import pytest
import aws_cdk as cdk

from cdk.naming import resource_prefix, ssm_path


def test_default_prefix_preserves_existing_resource_namespace():
    app = cdk.App()

    assert resource_prefix(app.node) == "eda"
    assert ssm_path(app.node, "network/VpcId") == "/eda/network/VpcId"


def test_custom_prefix_is_normalized_and_scopes_ssm_paths():
    app = cdk.App(context={"eda:stack_prefix": "Eda-Prod"})

    assert resource_prefix(app.node) == "eda-prod"
    assert ssm_path(app.node, "/storage/VolWorkId") == "/eda-prod/storage/VolWorkId"


@pytest.mark.parametrize(
    "prefix",
    ["---", "_", "1prod", "Eda_Prod", "Eda Prod", "E" * 49],
)
def test_invalid_prefix_is_rejected(prefix):
    app = cdk.App(context={"eda:stack_prefix": prefix})

    with pytest.raises(ValueError, match="must start with a letter"):
        resource_prefix(app.node)
