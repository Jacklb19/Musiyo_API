import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import make_session_factory
from app.main import create_app
from app.migrations import upgrade_database
from scripts.aws_demo import approved_configuration, require_free_plan
from scripts.demo import external_path


def test_free_account_guard_rejects_paid_expired_missing_or_low_credits():
    now = datetime.now(timezone.utc)
    state = dict(accountId='123456789012', accountPlanType='FREE', accountPlanStatus='ACTIVE',
                 accountPlanRemainingCredits={'amount': 100, 'unit': 'USD'}, accountPlanExpirationDate=now + timedelta(days=30))
    assert require_free_plan(state, '123456789012', Decimal(20), now) == state
    for patch in [{'accountPlanType': 'PAID'}, {'accountPlanStatus': 'EXPIRED'},
                  {'accountPlanRemainingCredits': {'amount': 0, 'unit': 'USD'}},
                  {'accountPlanExpirationDate': now - timedelta(seconds=1)}, {'accountId': '000000000000'},
                  {'accountPlanRemainingCredits': None}]:
        with pytest.raises(ValueError):
            require_free_plan(state | patch, '123456789012', Decimal(20), now)


def test_backups_and_environment_files_cannot_be_created_in_repository():
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError):
        external_path(str(root / 'private.env'))


def test_instance_approval_requires_ohio_and_concrete_owner_coverage(tmp_path):
    approval = dict(owner_approved=True, own_spend_limit_usd=0, region='us-east-2',
                    instance_type='t3.small', account_id='123456789012',
                    instance_id='i-0123456789abcdef0', eligible_services_verified=True,
                    responsible='Test owner')
    path = tmp_path / 'approval.json'
    path.write_text(json.dumps(approval), encoding='utf-8')
    assert approved_configuration(path) == approval
    for patch in ({'region': 'us-east-1'}, {'owner_approved': False},
                  {'own_spend_limit_usd': 1}, {'eligible_services_verified': False}):
        path.write_text(json.dumps(approval | patch), encoding='utf-8')
        with pytest.raises(ValueError):
            approved_configuration(path)


def test_readiness_checks_schema_without_migrating_at_startup(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    client = TestClient(create_app(make_session_factory(url)))
    assert client.get('/api/v1/health').status_code == 200
    assert client.get('/api/v1/ready').status_code == 503
    upgrade_database(url)
    assert client.get('/api/v1/ready').status_code == 200


def test_foundation_has_no_provider_invocation_or_duplicate_paid_services():
    root = Path(__file__).resolve().parents[1]
    template = json.loads((root / 'deployment/aws-foundation.cloudformation.json').read_text())
    types = [resource['Type'] for resource in template['Resources'].values()]
    assert types.count('AWS::EC2::Instance') == 1
    assert not set(types) & {'AWS::EC2::NatGateway', 'AWS::ElasticLoadBalancingV2::LoadBalancer', 'AWS::RDS::DBInstance'}
    assert 'bedrock:InvokeModel' not in json.dumps(template)
    properties = template['Resources']['Instance']['Properties']
    assert properties['CreditSpecification']['CPUCredits'] == 'standard'
    assert properties['MetadataOptions']['HttpTokens'] == 'required'
