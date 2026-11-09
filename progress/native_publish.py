"""Native Progress publication using existing Git reports and the private outbox."""

import hashlib
import json
import pathlib
import subprocess
from urllib.parse import urlencode

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import ForgeError, Incomplete, RepositoryRef
from tauceti_tools.git_credentials import scoped_git_environment
from tauceti_tools.source_identity import SourceActor

from . import apply, files, gh, publication_input
from .native_source import NativeSource, _number, _sha
from .provenance import MigrationMap


class NativePublisher:
    """Explicit reader/token/actor binding; no gh, shared-state or credential fallback."""

    def __init__(self, source, *, token, actor_id, timeout=120):
        if not isinstance(source, NativeSource):
            raise ValueError("native report publisher needs trusted content and roadmap bindings")
        roadmap = source.roadmap
        _number(actor_id, "publication actor")
        if type(timeout) not in (int, float) or not 0 < timeout <= 600:
            raise ValueError("native publication Git timeout must be bounded")
        if getattr(roadmap.api, "_token", None) != token:
            raise ValueError("publication API and Git must use the same explicitly supplied token")
        self.source, self.roadmap, self.api = source, roadmap, roadmap.api
        self.actor_id, self.timeout = actor_id, timeout
        self.environment = scoped_git_environment(roadmap.repository.forge, token)
        self.actor = self._check()

    def _check(self, identity=None):
        self.source.content.revalidate()
        self.roadmap.revalidate()
        actor = SourceActor.from_native(self.api.forge, self.api.current_user())
        if actor.principal.native_id != str(self.actor_id):
            raise Incomplete("native publication actor changed")
        if identity is not None:
            row = self.api.repository(identity.repository)
            if row.get("id") != identity.native_id or type(row.get("id")) is not int \
                    or RepositoryRef(self.api.forge, row.get("full_name")).key != identity.repository.key:
                raise Incomplete("native publication repository identity changed")
        return actor

    def _git(self, directory, identity, *arguments):
        self._check(identity)
        result = subprocess.run(["git", "-C", str(directory), *arguments], env=self.environment,
                                capture_output=True, text=True, timeout=self.timeout)
        self._check(identity)
        return result

    def _write(self, operation, path, data, reconcile, context):
        self._check()
        actor = f"{self.api.forge.key}/users/{self.actor_id}"
        args = ["POST" if operation[0] in {"fork", "create-report", "supersede-comment"} else "PATCH", path,
                json.dumps(data, sort_keys=True, separators=(",", ":"))]

        def send():
            self._check()
            try:
                self.api.request(args[0], path, data)
            except ForgeError:
                # A failed response may follow a successful native write.
                return subprocess.CompletedProcess(args, 1, "", "native outcome needs reconciliation")
            return subprocess.CompletedProcess(args, 0, "", "")

        def observe():
            self._check()
            result = reconcile()
            self._check()
            return result

        return gh.retained_write(args, origin=self.api.forge.key, actor=actor,
                                 operation=[self.roadmap.identity.record(), *operation], send=send,
                                 reconcile=observe, context={"source": self.roadmap.identity.record(), **context},
                                 verified=lambda _: False)

    def _fork(self):
        """Discover owned forks by native ancestry IDs, including renamed forks."""
        self._check()
        matches, uncertain, collision = [], False, False
        for row in self.api.collection("user/repos", counted=True):
            if not isinstance(row, dict):
                raise Incomplete("native fork catalog is unavailable")
            owner = row.get("owner")
            if not isinstance(owner, dict) or type(owner.get("id")) is not int:
                raise Incomplete("native fork ownership is unavailable")
            if owner["id"] != self.actor_id:
                continue
            ref = RepositoryRef(self.api.forge, row.get("full_name"))
            collision |= ref.name.split("/")[1].casefold() == self.roadmap.repository.name.split("/")[1].casefold()
            if type(row.get("fork")) is not bool:
                raise Incomplete("native fork ancestry is unavailable")
            if not row["fork"]:
                continue
            full = self.api.repository(ref)
            if full.get("id") != row.get("id") or type(full.get("id")) is not int \
                    or full.get("owner", {}).get("id") != self.actor_id \
                    or RepositoryRef(self.api.forge, full.get("full_name")).key != ref.key:
                raise Incomplete("native fork identity changed during discovery")
            identity = RepositoryIdentity(ref, _number(full.get("id"), "fork repository ID"))
            parent = full.get("parent")
            found = False
            for _ in range(8):
                if parent is None:
                    break
                parent_ref = RepositoryRef(self.api.forge, parent.get("full_name"))
                if parent.get("id") == self.roadmap.identity.native_id:
                    if parent_ref.key != self.roadmap.repository.key:
                        raise Incomplete("native fork parent identity changed")
                    found = True
                    break
                parent = self.api.repository(parent_ref).get("parent")
            else:
                uncertain = True
            if found:
                if full.get("permissions", {}).get("push") is not True or full.get("archived") is not False:
                    uncertain = True
                else:
                    self._check(identity)
                    matches.append(identity)
        self._check()
        if matches:
            return sorted(matches, key=lambda item: item.repository.key)[0], False
        if uncertain:
            return None, None
        return None, not collision

    def _target(self):
        row = self.api.repository(self.roadmap.repository)
        push = row.get("permissions", {}).get("push")
        if type(push) is not bool:
            raise Incomplete("native publication push permission is unavailable")
        if push:
            self._check()
            return self.roadmap.identity
        fork, absent = self._fork()
        if fork is not None:
            return fork
        if absent is not True:
            raise Incomplete("native fork absence or ownership remains uncertain")

        def reconcile():
            found, absent = self._fork()
            return found.repository.url if found else (False if absent else None)

        self._write(["fork"], self.roadmap.path + "/forks", {}, reconcile, {"actor": self.actor_id})
        fork, _ = self._fork()
        if fork is None:
            raise Incomplete("native fork creation has not become visible")
        return fork

    def _reports(self, branch):
        query = urlencode({"state": "all", "head": branch, "base": "main"})
        rows = self.api.collection(self.roadmap.path + "/pulls?" + query, counted=True)
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise Incomplete("native report inventory is unavailable")
            if row.get("state") not in {"open", "closed"} or type(row.get("merged")) is not bool:
                raise Incomplete("native report state is unavailable")
            head, base = row.get("head"), row.get("base")
            if not isinstance(head, dict) or not isinstance(base, dict):
                raise Incomplete("native report branch binding is unavailable")
            base_repo = base.get("repo")
            if not isinstance(base_repo, dict) or type(base_repo.get("id")) is not int \
                    or head.get("ref") != branch or base.get("ref") != "main" \
                    or base_repo["id"] != self.roadmap.identity.native_id:
                raise Incomplete("native report query returned a different branch or repository")
            repository = head.get("repo")
            if repository is None:
                raise Incomplete("native report head repository was deleted; reconcile before publication")
            if not isinstance(repository, dict) or type(repository.get("id")) is not int:
                raise Incomplete("native report head repository ID is unavailable")
            raw_owner = repository.get("owner")
            owner = raw_owner.get("id") if isinstance(raw_owner, dict) else None
            if type(owner) is not int:
                raise Incomplete("native report head owner is unavailable")
            if repository.get("id") == self.roadmap.identity.native_id or owner == self.actor_id:
                _number(row.get("number"), "report number")
                result.append(row)
        self._check()
        return result

    def run(self, plan, status_body, section_body, directory, *, dry_run=False, version=None):
        source = plan.get("source")
        if source != self.source.record():
            raise apply.ApplyError("prepared plan belongs to another trusted native source binding")
        publication_input.record(plan)
        directory = pathlib.Path(directory)
        self._check()
        main = self.api.request("GET", self.roadmap.path + "/git/commits/main?stat=false&verification=false&files=false")
        revision = _sha(main.get("sha"))
        fetched = self._git(directory, self.roadmap.identity, "fetch", "--no-tags", self.roadmap.repository.clone_url, revision)
        if fetched.returncode:
            raise apply.RemoteUnavailable("native roadmap base could not be fetched")
        apply._run(["git", "update-ref", "refs/remotes/origin/main", revision], directory)
        branch = apply.branch_name(plan)
        existing = self._reports(branch)
        if any(row["state"] == "open" or not row["merged"] for row in existing):
            opened = next((row for row in existing if row["state"] == "open"), None)
            if opened is not None and not dry_run:
                self._sweep_after_publication(plan, branch, self.roadmap.repository.pull_url(opened["number"]))
            return apply.EX_NOPROGRESS
        prepared, header = apply.prepare_report(plan, status_body, section_body, directory)
        if prepared:
            return prepared
        if dry_run:
            return 0
        target = self._target()
        remote = self._git(directory, target, "ls-remote", "--exit-code", "--heads", target.repository.clone_url,
                           "refs/heads/" + branch)
        if remote.returncode == 2:
            self._git(directory, target, "push", f"--force-with-lease=refs/heads/{branch}:",
                      target.repository.clone_url, "HEAD:refs/heads/" + branch)
            remote = self._git(directory, target, "ls-remote", "--exit-code", "--heads", target.repository.clone_url,
                               "refs/heads/" + branch)
        rows = [row.split() for row in remote.stdout.splitlines()]
        if remote.returncode or len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != "refs/heads/" + branch:
            raise apply.RemoteUnavailable("native report branch is not verifiably present")
        candidate = _sha(rows[0][0])
        if self._git(directory, target, "fetch", "--no-tags", target.repository.clone_url, candidate).returncode:
            raise apply.RemoteUnavailable("native report candidate could not be fetched")
        try:
            apply.validate_retained_report(plan, candidate, directory)
        except publication_input.InputMismatch as exc:
            raise apply.ApplyError(str(exc)) from None
        data = {"base": "main", "head": target.repository.name.split("/")[0] + ":" + branch,
                "title": apply.pr_title(plan), "body": apply.pr_body(plan, header, version)}

        def reconcile():
            found = self._reports(branch)
            for row in found:
                if row["head"]["repo"]["id"] != target.native_id:
                    continue
                if row["head"].get("sha") != candidate:
                    raise gh.PublicationRejected("native report head changed; preserve its prepared candidate")
                return self.roadmap.repository.pull_url(row["number"])
            return False

        present = reconcile()
        if isinstance(present, str):
            print(present)
            self._sweep_after_publication(plan, branch, present)
            return apply.EX_NOPROGRESS
        url = self._write(["create-report", target.native_id, branch, "main"], self.roadmap.path + "/pulls", data,
                          reconcile, {"candidate": candidate, "target": target.record(),
                                      "input": publication_input.digest(plan)})
        print(url)
        self._sweep_after_publication(plan, branch, url)
        return 0

    def _sweep_after_publication(self, plan, branch, url):
        try:
            self.sweep(plan, branch, url)
        except (ForgeError, gh.GhError) as exc:
            print(f"native report is open; superseded-report cleanup remains pending: {exc}")

    def sweep(self, plan, branch, replacement_url):
        self._check()
        live = files.cursor(self.roadmap.file_at(plan["progress_path"]) or "")
        rows = self.roadmap.open_reports()
        policy = publication_input.record(plan)["history_migration_sha256"]
        eligible = []
        for row in rows:
            metadata = apply.report_meta(row.get("body"))
            if not metadata or metadata.get("source") != plan["source"] \
                    or metadata.get("history_migration_sha256", MigrationMap().digest) != policy:
                continue
            if live is None and (not plan.get("bootstrapped") or metadata.get("from_sha") != plan["from_sha"]):
                continue
            eligible.append(row)
        owns = self.roadmap.report_owner_filter()
        obsolete = apply.superseded_prs(eligible, plan["roadmap"], live or plan["from_sha"], plan["from_sha"],
                                       branch, (), our_prs=plan["prs"], owner_filter=owns)
        for row, reason in obsolete:
            number = row["number"]
            path = self.roadmap.path + f"/issues/{number}/comments"
            identity = json.dumps([plan["source"], number, branch], sort_keys=True, separators=(",", ":"))
            marker = hashlib.sha256(identity.encode()).hexdigest()
            note = f"Superseded by {replacement_url}: {reason}.\n\n<!--tauceti-progress-sweep:v1 {marker}-->"

            def comment_visible(path=path, note=note, number=number):
                for comment in self.api.collection(path, counted=True):
                    author = comment.get("user")
                    if not isinstance(author, dict) or type(author.get("id")) is not int:
                        raise Incomplete("superseded-report comment ownership is unavailable")
                    if comment.get("body") == note and author["id"] == self.actor_id:
                        return self.roadmap.repository.pull_url(number) + "#issuecomment-" + str(comment["id"])
                return False

            if not comment_visible():
                self._write(["supersede-comment", number, branch], path, {"body": note}, comment_visible,
                            {"replacement": replacement_url, "reason": reason})

            def closed(number=number):
                current = self.api.pull(self.roadmap.repository, number)
                if current.get("state") not in {"open", "closed"}:
                    raise Incomplete("superseded native report state is unavailable")
                return self.roadmap.repository.pull_url(number) if current["state"] == "closed" else False

            if not closed():
                self._write(["close-report", number], self.roadmap.path + f"/pulls/{number}", {"state": "closed"},
                            closed, {"replacement": replacement_url, "reason": reason})
