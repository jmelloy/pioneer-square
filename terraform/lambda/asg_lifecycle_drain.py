import json
import os
import time
import urllib.error
import urllib.request

import boto3

asg = boto3.client("autoscaling")


def _post_json(url: str, token: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "content-type": "application/json",
            "x-pioneer-lifecycle-token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handler(event, context):
    detail = event.get("detail") or {}
    instance_id = detail.get("EC2InstanceId")
    asg_name = detail.get("AutoScalingGroupName")
    hook_name = detail.get("LifecycleHookName")
    action_token = detail.get("LifecycleActionToken")

    if not all([instance_id, asg_name, hook_name, action_token]):
        print("Ignoring non-ASG lifecycle event:", json.dumps(event))
        return {"ignored": True}

    backend_url = os.environ["PIONEER_BACKEND_URL"].rstrip("/")
    guild_id = os.environ["PIONEER_GUILD_ID"]
    token = os.environ["PIONEER_ASG_LIFECYCLE_TOKEN"]
    drain_url = f"{backend_url}/internal/asg-lifecycle/{guild_id}/drain"

    deadline = time.time() + min(int(os.environ.get("DRAIN_TIMEOUT_SECONDS", "780")), 840)
    poll_seconds = int(os.environ.get("DRAIN_POLL_SECONDS", "15"))
    last = None

    while time.time() < deadline:
        try:
            last = _post_json(
                drain_url,
                token,
                {"instance_id": instance_id, "reason": "asg-lifecycle-terminating"},
            )
            print("drain status:", json.dumps(last, sort_keys=True))
            if last.get("ready"):
                break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"backend drain HTTP {exc.code}: {body}")
        except Exception as exc:
            print(f"backend drain failed: {exc!r}")

        try:
            asg.record_lifecycle_action_heartbeat(
                AutoScalingGroupName=asg_name,
                LifecycleHookName=hook_name,
                LifecycleActionToken=action_token,
                InstanceId=instance_id,
            )
        except Exception as exc:
            print(f"heartbeat failed: {exc!r}")
        time.sleep(poll_seconds)

    asg.complete_lifecycle_action(
        AutoScalingGroupName=asg_name,
        LifecycleHookName=hook_name,
        LifecycleActionToken=action_token,
        InstanceId=instance_id,
        LifecycleActionResult="CONTINUE",
    )
    return {"completed": True, "last": last}
