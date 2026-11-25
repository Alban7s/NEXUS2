"""Rollback/rollout logic against a FAKE Kubernetes API (no cluster).
These prove NEXUS's logic; a real-cluster test is listed separately as BLOCKED."""
import unittest
from types import SimpleNamespace as NS

from backend.errors import PipelineError
from backend.services import k8s_ops as k


def rs(rev, image, uid="dep-uid", ready=0, current=False):
    return NS(
        metadata=NS(annotations={k.REVISION_ANNOTATION: str(rev)}, creation_timestamp=None,
                    owner_references=[NS(uid=uid)], name=f"rs{rev}"),
        spec=NS(template=NS(spec=NS(containers=[NS(image=image)]), metadata=NS(labels={"app": "web", k.HASH_LABEL: "abc"}))),
        status=NS(replicas=ready, ready_replicas=ready))


class FakeCluster:
    """Behaves like a tiny cluster: patching the template changes the pod image."""
    def __init__(self, images, current=3, heal=True):
        self.sets = {i + 1: img for i, img in enumerate(images)}
        self.current = current
        self.heal = heal
        self.patches = []
        self.generation = 1
        self.observed = 1
        self.updated = 2

    def read_namespaced_deployment(self, name, ns):
        ready = 2 if self.heal or not self.patches else 0
        return NS(
            metadata=NS(uid="dep-uid", generation=self.generation, annotations={k.REVISION_ANNOTATION: str(self.current)}),
            spec=NS(replicas=2, selector=NS(match_labels={"app": "web"}),
                    template=NS(spec=NS(containers=[NS(image=self.sets[self.current])]))),
            status=NS(observed_generation=self.generation, updated_replicas=2 if ready else 0, replicas=2,
                      available_replicas=ready, ready_replicas=ready))

    def list_namespaced_replica_set(self, ns, label_selector=None):
        return NS(items=[rs(r, img) for r, img in self.sets.items()])

    def patch_namespaced_deployment(self, name, ns, body):
        self.patches.append(body)
        value = next(op["value"] for op in body if op["path"] == "/spec/template")
        rev = next(r for r, img in self.sets.items() if img == value["spec"]["containers"][0]["image"])
        self.current = rev
        self.generation += 1
        self.observed = self.generation

    def list_namespaced_pod(self, ns, label_selector=None):
        n = 2
        img = self.sets[self.current]
        ready = self.heal
        return NS(items=[NS(metadata=NS(deletion_timestamp=None), spec=NS(containers=[NS(image=img)]),
                            status=NS(container_statuses=[NS(ready=ready)])) for _ in range(n)])


def sanitize(template):
    return {"metadata": {"labels": dict(template.metadata.labels)},
            "spec": {"containers": [{"image": c.image} for c in template.spec.containers]}}


class Rollback(unittest.TestCase):
    def test_revisions_listed_newest_first_with_current_flag(self):
        c = FakeCluster(["site:1", "site:2", "site:3"])
        revs = k.list_revisions(c, "web", "default")
        self.assertEqual([r["revision"] for r in revs], [3, 2, 1])
        self.assertTrue(revs[0]["current"])
        self.assertFalse(revs[1]["current"])

    def test_successful_rollback_is_verified_against_pods(self):
        c = FakeCluster(["site:1", "site:2", "site:3"])
        logs = []
        facts = k.rollback_deployment(c, c, "web", "default", 2, logs.append, sanitize, timeout_s=5, sleep=lambda s: None)
        self.assertTrue(facts["verified"])
        self.assertEqual(facts["images"], ["site:2"])
        self.assertEqual(facts["podsReady"], 2)
        patch = c.patches[0]
        self.assertEqual(patch[0]["op"], "replace")
        self.assertNotIn(k.HASH_LABEL, patch[0]["value"]["metadata"]["labels"])

    def test_unhealthy_rollback_is_never_reported_as_success(self):
        c = FakeCluster(["site:1", "site:2", "site:3"], heal=False)
        with self.assertRaises(PipelineError) as ctx:
            k.rollback_deployment(c, c, "web", "default", 2, lambda m: None, sanitize, timeout_s=0, sleep=lambda s: None)
        self.assertEqual(ctx.exception.code, "rollback_unverified")

    def test_unknown_and_current_revisions_rejected(self):
        c = FakeCluster(["site:1", "site:2", "site:3"])
        for rev, code in ((9, "unknown_revision"), (3, "already_current")):
            with self.assertRaises(PipelineError) as ctx:
                k.rollback_deployment(c, c, "web", "default", rev, lambda m: None, sanitize, timeout_s=1)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(c.patches, [])


class RolloutRule(unittest.TestCase):
    def dep(self, **st):
        base = dict(observed_generation=2, updated_replicas=2, replicas=2, available_replicas=2)
        base.update(st)
        return NS(metadata=NS(generation=2), spec=NS(replicas=2), status=NS(**base))

    def test_complete(self):
        self.assertTrue(k.rollout_complete(self.dep())[0])

    def test_incomplete_cases(self):
        self.assertFalse(k.rollout_complete(self.dep(observed_generation=1))[0])
        self.assertFalse(k.rollout_complete(self.dep(updated_replicas=1))[0])
        self.assertFalse(k.rollout_complete(self.dep(replicas=3))[0])
        self.assertFalse(k.rollout_complete(self.dep(available_replicas=1))[0])


if __name__ == "__main__":
    unittest.main()
