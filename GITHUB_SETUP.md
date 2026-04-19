# GitHub Setup Walkthrough (Beginner-Friendly)

This guide takes you from "I have a folder on my computer" to "my project is live on GitHub." Pick one of two paths:

- **Path A — GitHub Desktop (GUI):** click-through, no terminal. Best if you're new to command lines.
- **Path B — Command line (terminal):** more powerful, becomes the standard once you're comfortable. Recommended long-term.

You only need to do **one** of these paths. Everything after the push is the same.

---

## 0. Key concepts (2-minute read)

Before you touch any tool, these five words will make everything else make sense.

| Term | What it is |
|---|---|
| **Repository ("repo")** | A project folder that git is tracking. Lives on your computer AND on GitHub — they sync. |
| **Commit** | A saved snapshot of your files + a short message describing what changed. The unit of history. |
| **Branch** | A named line of history. `main` is the default trunk. You'll stay on `main` for a while. |
| **Push** | Upload your local commits to GitHub. |
| **Pull** | Download GitHub's commits to your local copy. |

That's it. Git has a LOT of other features, but you don't need them yet.

---

## 1. Rename your folder (do this first, both paths)

Your desktop folder is currently named **"EDGAR API"** (with a space). GitHub repos use lowercase-with-hyphens by convention and spaces in folder names break command-line tools later. Rename it now:

- **Mac:** right-click the folder → Rename → type `edgar-api-data-project`
- **Windows:** right-click → Rename → type `edgar-api-data-project`

> Why hyphens, not spaces or underscores? Hyphens are URL-safe, read well in links (`github.com/you/edgar-api-data-project`), and they're the convention across open-source. Underscores are fine too — pick one and stay consistent.

---

## 2. Create the empty repo on GitHub

1. Go to <https://github.com> and sign in.
2. Click the **+** in the top-right → **New repository**.
3. Fill in the form:
   - **Repository name:** `edgar-api-data-project` (match the folder you just renamed)
   - **Description:** `Data project built on the SEC EDGAR public APIs.`
   - **Public** (you picked this earlier)
   - **DO NOT check "Add a README file"**, "Add .gitignore", or "Choose a license" — we already have those locally, and adding them here creates conflicts for first-timers.
4. Click **Create repository**.

You'll land on a page titled "Quick setup — if you've done this kind of thing before." Leave that tab open; you'll copy the URL from it in a moment. It looks like:

```
https://github.com/<your-username>/edgar-api-data-project.git
```

---

## Path A — GitHub Desktop (GUI, no terminal)

### A.1 Install GitHub Desktop

Download from <https://desktop.github.com> and sign in with your GitHub account.

### A.2 Add your local folder

1. In GitHub Desktop: **File** → **Add local repository** → browse to your renamed `edgar-api-data-project` folder → **Add Repository**.
2. It will warn: "This directory does not appear to be a Git repository." Click **create a repository** in that warning.
3. In the dialog:
   - **Name:** `edgar-api-data-project`
   - **Description:** same as above
   - **Local path:** (already filled in)
   - **Initial branch:** `main`
   - **Git ignore:** leave as None (you already have a `.gitignore`)
   - **License:** None (you can add MIT later if you want)
4. Click **Create Repository**.

### A.3 First commit

In the left panel you'll see every file in your folder listed as "changes." At the bottom-left:

1. Summary: `Initial commit: project scaffolding and EDGAR docs`
2. Click **Commit to main**.

### A.4 Publish to GitHub

Click **Publish repository** in the top bar. Make sure:
- **Name:** `edgar-api-data-project`
- **Keep this code private:** UNCHECKED (so it's public, as you chose)

Click **Publish repository**. Done — open the GitHub tab you left open in Step 2, refresh, and your files are there.

**Skip to Section 4.**

---

## Path B — Command line (terminal)

### B.1 Check if git is installed

Open Terminal (Mac: Cmd+Space → "Terminal"; Windows: Start → "Git Bash" after installing below).

```bash
git --version
```

- If it prints something like `git version 2.x.x`, you're set.
- If not:
  - **Mac:** run `xcode-select --install` and follow the prompt.
  - **Windows:** download from <https://git-scm.com/download/win> and run the installer. Defaults are fine. Use "Git Bash" as your terminal afterward.

### B.2 One-time git identity setup

Git stamps every commit with a name and email. Run these two commands once, replacing with your info:

```bash
git config --global user.name "Dev"
git config --global user.email "devonlara13@gmail.com"
```

> Use the same email as your GitHub account so commits display with your avatar on github.com.

### B.3 Set the default branch name

```bash
git config --global init.defaultBranch main
```

Newer GitHub uses `main` (not `master`) as the default — this makes sure local git matches.

### B.4 Initialize the repo inside your folder

```bash
cd ~/Desktop/edgar-api-data-project    # or wherever you put it
git init
git add .
git status                             # preview what will be committed
git commit -m "Initial commit: project scaffolding and EDGAR docs"
```

### B.5 Connect to GitHub and push

Copy the URL from the GitHub page you left open in Step 2, then:

```bash
git branch -M main
git remote add origin https://github.com/<your-username>/edgar-api-data-project.git
git push -u origin main
```

GitHub will prompt you to authenticate. **Do NOT use your password** — GitHub disabled that years ago. Instead:

- **Easiest:** install the [GitHub CLI](https://cli.github.com) (`brew install gh` on Mac, `winget install GitHub.cli` on Windows), then run `gh auth login` once. All subsequent pushes are authenticated automatically.
- **Alternative:** generate a Personal Access Token at <https://github.com/settings/tokens> → "Generate new token (classic)" → check the `repo` scope → paste the token when git asks for a password. Save it in your OS keychain/credential manager.

Once `git push` succeeds, refresh your GitHub tab — your files are live.

---

## 3. Verify it worked

Go to `https://github.com/<your-username>/edgar-api-data-project` and confirm:

- The README renders as the landing page (formatted with headings, the structure tree, etc.).
- The `docs/` folder shows your five documentation files.
- The `.gitignore` file is present.
- The `data/`, `src/`, `notebooks/`, `tests/` folders each contain a `.gitkeep` (empty placeholder) file.

If you click on `docs/edgar-api-erd.md`, GitHub will even render the Mermaid diagram inline.

---

## 4. The daily workflow (what you'll do from now on)

Every time you make changes, the loop is the same three steps:

### Path A (GitHub Desktop)
1. Save your files in your editor.
2. Open GitHub Desktop — your changes show up automatically.
3. Write a commit summary, click **Commit to main**, then **Push origin**.

### Path B (terminal)
```bash
git status                    # what changed?
git add .                     # stage everything (or list specific files)
git commit -m "Add notebook exploring AAPL filings"
git push
```

### Writing good commit messages

Short, imperative mood, describe the *why* when it's not obvious from the diff:

Good:
- `Add rate-limited EDGAR client with User-Agent config`
- `Fix CIK zero-padding for non-numeric input`
- `Update ERD to mark FRAME as optional on XBRL_FACT`

Less good:
- `stuff`
- `updated file`
- `asdf`

---

## 5. Best practices cheat sheet

A handful of rules that will save you pain later:

**Do:**
- Commit often. Many small commits > one giant commit. Easier to review and to revert.
- Write the commit message as if completing the sentence *"If applied, this commit will..."*.
- Keep your `main` branch working. When you start experimenting, create a branch: `git checkout -b feature/try-xbrl-frames`.
- Put anything large, private, or generated in `.gitignore`. Data files, API keys, `.env`, cache folders.

**Don't:**
- Commit secrets. If you accidentally commit an API key, **rotate the key immediately** — git history is forever, and the whole internet can see a public repo.
- Commit huge binary data files. GitHub caps files at 100 MB. For data, consider DVC or just keeping it in `data/` (which is gitignored).
- Force-push to `main` (`git push --force`). Until you know exactly what that does, don't.
- Edit commits that have been pushed. Create a new one.

**Secrets handling (important for this project):**
Your SEC User-Agent isn't a secret — it's literally required to be `Your Name your@email.com` in every request. But you probably don't want your personal email in a public repo. Two options:

1. **Use an environment variable** (what `src/edgar_client.py` does). Create a `.env` file in the project root with:
   ```
   SEC_USER_AGENT=Dev Lara devonlara13@gmail.com
   ```
   The existing `.gitignore` excludes `.env`, so this never gets committed.

2. **Commit a non-personal identifier.** Something like `edgar-api-data-project github.com/your-username` is perfectly valid to SEC and fine to commit.

---

## 6. What to do next

Once the repo is up:

1. **Add a repository description and topics** on GitHub (top-right of the repo page → ⚙️ icon next to "About"). Topics like `sec`, `edgar`, `finance-data`, `python` help people discover it.
2. **Pin the repo** to your profile (on your profile page → Customize pins).
3. **Start the first notebook.** `cd notebooks && jupyter notebook` → create `01-explore-submissions.ipynb` → commit → push.
4. **Consider adding a LICENSE file later** if you want others to be able to reuse your code. MIT is the standard permissive choice; without one, technically "all rights reserved" applies, which limits what others can do with your code. GitHub can add one for you: your repo → **Add file** → **Create new file** → type `LICENSE` → click "Choose a license template."

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `fatal: not a git repository` | You're in the wrong folder. `cd` into `edgar-api-data-project` first. |
| `git push` asks for password | You can't use a GitHub password for git. Use `gh auth login` (GitHub CLI) or a Personal Access Token. |
| `rejected — non-fast-forward` | GitHub has commits yours doesn't. Run `git pull --rebase` first, then push again. |
| You accidentally committed a secret | Rotate the secret immediately, then see [GitHub's guide to removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository). |
| You committed a huge file and push is rejected | Remove it, add it to `.gitignore`, and look up `git filter-repo` to scrub history. |
| `.DS_Store` / Thumbs.db keeps appearing | They're in the `.gitignore` already. If they were already tracked before the gitignore existed, run `git rm --cached .DS_Store`, commit, push. |

---

## 8. Resources when you want to go deeper

- [**GitHub's Hello World tutorial**](https://docs.github.com/en/get-started/quickstart/hello-world) — 10-minute walkthrough of a PR
- [**Pro Git book (free online)**](https://git-scm.com/book/en/v2) — the definitive reference, readable front-to-back
- [**Oh Shit, Git!?!**](https://ohshitgit.com) — plain-language recipes for common mess-ups
- [**Conventional Commits**](https://www.conventionalcommits.org) — a style guide for commit messages once you're ready

---

**You're done.** The repo is live, documented, and set up to grow. Next request: start a notebook in `notebooks/` and make your first real commit.
