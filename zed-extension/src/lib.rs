use zed_extension_api::{self as zed, LanguageServerId, Result};

struct OverleafSyncExtension;

impl zed::Extension for OverleafSyncExtension {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        _language_server_id: &LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<zed::Command> {
        let command = worktree.which("overleaf-sync-lsp").ok_or_else(|| {
            "overleaf-sync-lsp not found in PATH; install it with `pipx install overleaf-sync` \
             (or pip install -e . from the repository)"
                .to_string()
        })?;
        Ok(zed::Command {
            command,
            args: vec![],
            env: worktree.shell_env(),
        })
    }
}

zed::register_extension!(OverleafSyncExtension);
