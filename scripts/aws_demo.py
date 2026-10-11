"""Operate an owner-approved instance; never provision or upgrade an AWS account."""
import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.config import Config


def require_free_plan(state, account_id: str, required_credits: Decimal, now=None):
    now = now or datetime.now(timezone.utc)
    balance = state.get('accountPlanRemainingCredits') or {}
    expiration = state.get('accountPlanExpirationDate')
    if (state.get('accountId') != account_id or state.get('accountPlanType') != 'FREE'
            or state.get('accountPlanStatus') != 'ACTIVE' or balance.get('unit') != 'USD'
            or Decimal(str(balance.get('amount', 0))) < required_credits
            or not isinstance(expiration, datetime) or expiration <= now):
        raise ValueError('An active Free account plan with verified USD credits is required; remain local')
    return state


def approved_configuration(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if value.get('owner_approved') is not True or value.get('own_spend_limit_usd') != 0:
        raise ValueError('Concrete resources and coverage must be approved by the owner first')
    if value.get('region') != 'us-east-1' or value.get('instance_type') != 't3.small':
        raise ValueError('Resource configuration differs from the demonstration proposal')
    import re
    if not re.fullmatch(r'[0-9]{12}', value.get('account_id', '')) or not re.fullmatch(r'i-[a-f0-9]{8,17}', value.get('instance_id', '')):
        raise ValueError('Invalid approved account or instance identifier')
    if not value.get('eligible_services_verified') or not value.get('responsible'):
        raise ValueError('Owner coverage evidence and responsible party are required')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['status', 'start', 'stop'])
    parser.add_argument('--approval', required=True, help='External owner approval JSON')
    args = parser.parse_args()
    approval = approved_configuration(args.approval)
    session = boto3.Session(region_name=approval['region'])
    config = Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 1})
    identity = session.client('sts', config=config).get_caller_identity()
    if identity['Account'] != approval['account_id']:
        parser.error('The SDK identity differs from the approved account')
    ec2 = session.client('ec2', config=config)
    instance = ec2.describe_instances(InstanceIds=[approval['instance_id']])['Reservations'][0]['Instances'][0]
    tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
    if instance['InstanceType'] != approval['instance_type'] or tags.get('Project') != 'Musiyo':
        parser.error('The selected instance differs from the approved Musiyo resource')
    if args.operation == 'stop':
        # Stopping remains possible when credits expire; it never deletes persistent storage.
        ec2.stop_instances(InstanceIds=[approval['instance_id']])
        print(json.dumps({'operation': 'stop_requested'}))
        return
    state = session.client('freetier', config=config).get_account_plan_state()
    require_free_plan(state, approval['account_id'], Decimal(str(approval.get('minimum_remaining_credits_usd', 20))))
    if args.operation == 'start':
        ec2.start_instances(InstanceIds=[approval['instance_id']])
    print(json.dumps({'operation': args.operation, 'instance_state': instance['State']['Name'],
                      'plan': state['accountPlanType'], 'expiration': state['accountPlanExpirationDate'].isoformat()}))


if __name__ == '__main__':
    main()
