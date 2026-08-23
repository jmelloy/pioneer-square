"""EC2 Auto Scaling scale-in protection for the warm ASG worker fleet.

The warm worker fleet (terraform/asg_workers.tf) runs each worker as a plain
EC2 instance in an Auto Scaling Group — not as an ECS task, so it gets none
of the automatic instance protection that ``managed_termination_protection``
gives the on-demand ECS RunTask fleet (terraform/ecs_capacity.tf). The ASG
termination lifecycle hook (aws_autoscaling_lifecycle_hook.worker_terminating
+ terraform/lambda/asg_lifecycle_drain.py) already pauses termination of an
instance picked for scale-in until its worker finishes draining, but nothing
stops the ASG from picking a *busy* instance over an idle one in the first
place. Setting ``ProtectedFromScaleIn`` while an agent is busy fixes that:
scale-in only ever targets instances that are already idle.

A no-op everywhere except the ASG fleet: PIONEER_ASG_NAME is only set by
terraform/templates/worker_user_data.sh.tpl, so docker-compose and ECS
RunTask workers skip this entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def is_asg_worker() -> bool:
    return bool(os.environ.get("PIONEER_ASG_NAME"))


def _set_protection_sync(*, asg_name: str, instance_id: str, protect: bool) -> None:
    import boto3

    client = boto3.client("autoscaling")
    client.set_instance_protection(
        InstanceIds=[instance_id],
        AutoScalingGroupName=asg_name,
        ProtectedFromScaleIn=protect,
    )


async def set_scale_in_protection(instance_id: str, protect: bool) -> None:
    """Enable/disable ASG scale-in protection for this instance.

    No-op unless PIONEER_ASG_NAME is set. Failures are logged and swallowed:
    this is a best-effort safety net layered on top of the termination
    lifecycle hook, which already guards against a busy instance actually
    being terminated — it must never affect task execution.
    """
    asg_name = os.environ.get("PIONEER_ASG_NAME")
    if not asg_name:
        return
    try:
        await asyncio.to_thread(
            _set_protection_sync, asg_name=asg_name, instance_id=instance_id, protect=protect
        )
        logger.info(
            "asg-protection: %s scale-in protection for instance %s (asg=%s)",
            "enabled" if protect else "disabled",
            instance_id,
            asg_name,
        )
    except ImportError:
        logger.warning("boto3 not installed — ASG scale-in protection disabled")
    except Exception:
        logger.warning(
            "asg-protection: failed to set scale-in protection=%s for instance %s",
            protect,
            instance_id,
            exc_info=True,
        )
