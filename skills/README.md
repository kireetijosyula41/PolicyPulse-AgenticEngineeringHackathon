# PolicyPulse Agent Skills

Skills are discoverable application instruction packages. The runtime loads them through `app.agent.skills.SkillLoader`; a skill never grants authorization or adds tools. Tool availability and source access remain controlled by the trusted application context and `ToolRegistry`.

To add a fourth skill without changing the agent runtime:

1. Create `skills/<lowercase-kebab-name>/SKILL.md`.
2. Add front matter with `name`, semver `version`, concise `description`, `activation_triggers`, optional `counterexamples`, and `required_tools`.
3. Keep `name` identical to the directory name and list only names already exposed by `ToolRegistry`.
4. Include activation criteria, prerequisites, workflow, structured output, abstention rules, an example, and security/privacy constraints.
5. Run `pytest -q`; discovery rejects invalid metadata, missing tools, unsafe instruction patterns, symlinks, and oversized files.
6. Select it explicitly with `requested_skill` or let bounded trigger/classifier selection choose it. No Python runtime change is required.

