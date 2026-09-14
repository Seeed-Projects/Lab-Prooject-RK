# GitHub publishing checklist

This project is published as the standalone
`Seeed-Projects/Lab-Prooject-RK` repository.

## Before publishing

1. Read `THIRD_PARTY_NOTICES.md` and confirm redistribution rights for the RKNN files, sample video/WAV files, screenshots, and Rockchip wheel.
2. Run `./scripts/verify.sh` on the target RK3588 after `./scripts/bootstrap.sh`.
3. Create an empty GitHub repository without a generated README, license, or
   `.gitignore`; those files already exist locally.

## Create the first commit

```bash
cd /home/seeed/recomputer-rk3588-retail-ai-suite
git add .
git status
git commit -m "Initial RK3588 retail AI demo suite"
```

Attach the empty GitHub repository and push `main`:

```bash
git remote add origin git@github.com:Seeed-Projects/Lab-Prooject-RK.git
git push -u origin main
```

No tracked file should exceed GitHub's 100 MB per-file limit. Do not force-add `.env`, `models/piper`, `frontend/node_modules`, `frontend/dist`, backend runtime data, or the voice pipeline offline bundle.
