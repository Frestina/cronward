# Security

cronward holds GitHub tokens that can start workflows, so a flaw in it can
matter. Please report one privately, not in a public issue.

## How to report

Use GitHub's private vulnerability reporting: the **Security** tab of this
repo, then **Report a vulnerability**. Say what you found, how to reproduce
it, and which version (`cronward --version`). Don't paste a real token.

You get an answer within a week. cronward is maintained by one person in
their spare time, so a fix can take longer; you will hear how it is going,
and you get credit in the release notes unless you'd rather not.

## In scope

The rules in [SPEC.md](SPEC.md#what-must-never-happen), and anything that
breaks them:

- A token leaking: into the config, the state, a log, the output, a notice,
  or to any host but `api.github.com`.
- Tokens stored so that another user on the machine can read them.
- cronward talking to a host other than `api.github.com`, for example by
  following a redirect.
- Code or commands from a repo, a workflow or GitHub's answers running on
  the machine, or text from GitHub or a viewer's server reaching a terminal
  or a notification without being cleaned (escape sequences, markup).
- A run that didn't happen shown as passed, or a slot started twice.

## Not in scope

- What a workflow does when cronward starts it: that runs on GitHub, under
  the repo's own rules.
- Someone who can already act as your user on the machine. They can read
  your files, so they can read a token that is not encrypted (systemd older
  than 256), and they can use `systemd-creds` as you.
- Your `notify.command` and the viewer's ssh: both are yours to set up.

## Supported versions

Only the latest release gets fixes.
