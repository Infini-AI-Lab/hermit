"""Transfer the skill set selected by the pinned loop with its file bytes and executable modes intact."""
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from libs.terminus_agent.agents.terminus_2.harbor_terminus_2_evolution import HarborTerminus2Evolution
from libs.terminus_agent.evolution.skill_schema import validate_skill_directory

from hermit.agent.evolver.methods.coevoskills_adapter.integrity import _files
from hermit.agent.evolver.methods.coevoskills_adapter.upstream import real_tree, safe_name


def copy_package(source, destination):
    """Copy the upstream package layout (manifest, scripts, references), excluding interpreter caches."""
    real_tree(source)
    if issues := validate_skill_directory(source):
        raise ValueError(f"invalid CoEvoSkills package: {issues[0].message}")
    destination.mkdir()
    shutil.copy2(source / "SKILL.md", destination / "SKILL.md")
    for name in ("scripts", "references"):
        if (source / name).exists():
            shutil.copytree(source / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


class HermitEvolutionAgent(HarborTerminus2Evolution):
    def __init__(self, *args, hermit_export, **kwargs):
        super().__init__(*args, **kwargs)
        self.hermit_export = Path(hermit_export)

    async def _export_skills_from_container(self, environment, skill_names, target_dir, include_references=False):
        with TemporaryDirectory(dir=self.hermit_export.parent) as temporary:
            snapshot = Path(temporary) / "snapshot"
            snapshot.mkdir()
            for name in sorted(skill_names):
                container_path = f"/app/environment/skills/{safe_name(name)}"
                # Harbor's Docker download_dir copies a directory into its existing target. Two matching
                # snapshots retain the upstream stable-read check without decoding binaries or losing modes.
                for attempt in (1, 2):
                    local = Path(temporary) / f"{name}-{attempt}"
                    local.mkdir()
                    await environment.download_dir(container_path, local)
                    real_tree(local / name)
                first, last = (Path(temporary) / f"{name}-{i}" / name for i in (1, 2))
                if _files(first) != _files(last):
                    raise RuntimeError(f"the skill changed during export: {name}")
                copy_package(last, snapshot / name)
            if include_references:  # Upstream's oracle/selection snapshot; its target is a fresh directory.
                for name in sorted(skill_names):
                    shutil.move(snapshot / name, target_dir / name)
            else:  # The loop is exporting its current final skills to the host.
                self._rollback_host_skills(environment, snapshot)
            return sorted(skill_names)

    async def _inject_skill_dir_to_container(self, name, skill_dir, environment):
        if not name.startswith("evo-"):  # Keep the pinned meta-skill injection.
            return await super()._inject_skill_dir_to_container(name, skill_dir, environment)
        destination = f"/app/environment/skills/{safe_name(name)}"
        with TemporaryDirectory(dir=self.hermit_export.parent) as temporary:
            package = Path(temporary) / name
            copy_package(skill_dir, package)
            made = await environment.exec(command=f"mkdir -p /app/environment/skills && rm -rf -- '{destination}'",
                                          timeout_sec=30)
            if made.return_code:
                raise RuntimeError(f"could not prepare the container skill directory for {name}")
            await environment.upload_dir(package, destination)
        return True

    def _rollback_host_skills(self, environment, snapshot_skills_dir):
        # Upstream also calls this when it chooses an earlier best version. Its host tree can contain
        # unrelated packages from the last round. Publish exactly this snapshot in a separate directory.
        if self.hermit_export.exists():
            shutil.rmtree(self.hermit_export)
        super()._rollback_host_skills(environment, snapshot_skills_dir)
        with TemporaryDirectory(dir=self.hermit_export.parent) as temporary:
            candidate = Path(temporary) / "skills"
            shutil.copytree(snapshot_skills_dir, candidate)
            candidate.rename(self.hermit_export)
