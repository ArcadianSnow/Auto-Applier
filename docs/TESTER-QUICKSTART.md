# Auto Applier — tester quick start

Thanks for trying this. It's **alpha software** (`3.0.0a0`) that touches real job applications,
so this page is deliberately blunt about what it will and won't do.

**The one thing to know up front: out of the box it does not submit anything.** It finds jobs,
scores them, and writes a tailored résumé + cover letter. Actually sending an application is a
separate, deliberate switch. You can't trip it by clicking around.

---

## What you need

| | |
|---|---|
| **Windows 10/11** | The installer is Windows-only. |
| **~15 GB free disk** | Most of it is the AI model (see below). |
| **16 GB RAM** | 8 GB will run but the AI step will be slow. |
| **Google Chrome** | It drives your real Chrome. Already have it? You're fine. |
| **Ollama** | The local AI. Free, from [ollama.com](https://ollama.com/download). The app will tell you if it's missing. |

Everything runs **on your machine**. No account, no cloud, no cost. Nothing about you leaves
your computer unless you explicitly turn on error reporting in Step 7.

---

## 1. Install

Double-click **`AutoApplier-Setup-3.0.0a0.exe`**.

> ### ⚠️ Windows will try to stop you
> You'll see a blue box: **"Windows protected your PC."**
>
> Click **More info** → **Run anyway**.
>
> This is expected. It appears because the installer isn't code-signed (a certificate costs a
> few hundred dollars a year and this is a personal project). It is not a virus warning — it
> means Windows doesn't recognise the publisher. If that's not good enough for you, don't
> install it; that's a completely reasonable call.

It installs to your user folder — **no admin password needed**, and it won't touch anything
else on your system.

## 2. First launch

Launch it from the Start Menu. A browser tab opens with the dashboard, and a banner says
**"Finish onboarding to start the scheduler — open the wizard."** Click that.

The wizard has 10 steps. **Steps 1–7 are the ones it needs**; 8, 9 and 10 are skippable.
(Step 7 only needs a yes/no from you — it just won't let you skip *deciding*.)

1. **Set up the AI engine** — downloads the AI models. **This is the slow part: about 10 GB.**
   Start it and go make coffee. It only happens once. If Ollama isn't installed it'll point you
   at the download first.
2. **Contact** — name, email, phone, location.
3. **Work history** — your jobs. This is what everything else is built from, so it's worth
   doing properly.
4. **Skills**
5. **Work authorization** — are you allowed to work where you're applying, do you need a visa.
6. **Targeting** — job titles you want, locations, minimum salary. There's a **Guided setup**
   button on the dashboard if you'd rather describe what you want in plain English.
7. **Telemetry** — do you want errors sent to me automatically? **Default is no.** Either answer
   is fine; it just wants you to choose rather than silently defaulting you in.
8–10. Email, control preferences, extra details — **optional, skip them.**

Then it starts finding jobs. Give it a while; the first pass has a lot to chew through.

---

## 3. What it actually does

**Finds** jobs on Greenhouse, Lever and Ashby job boards → **scores** each one against your
history → **writes** a résumé and cover letter tailored to that specific job → **fills in** the
application form.

Two things it will never do:

- **It won't make things up about you.** Every line of a generated résumé has to trace back to
  something you entered. If the AI can't support a claim, the job stops and waits for you
  instead of inventing experience. Same for form questions — if it isn't confident, it hands it
  to you rather than guessing.
- **It won't answer for you on anything personal.** Diversity/EEO questions, "do you consent
  to…", "are you a human" — those always come to you.

### Submitting for real

Off by default. When you do turn it on, most applications still stop at **"assisted"**: the bot
fills the whole form, then hands you the browser to read it over and click Submit yourself.
That's the intended mode, not a fallback.

---

## 4. When something breaks

**Click "Report a problem" at the bottom of any page.** It builds a file and downloads it —
send me that file plus one line about what you were doing.

The file contains error logs, your settings, and a health check. **It does not contain your
answers, your résumé, or anything you typed about yourself** — that's stripped out on purpose.
You can open it and look; it's just a zip.

Also useful to me:
- what you expected vs. what happened
- a screenshot if it's a visual thing

### Things that are normal, not bugs

- **The first AI step takes 10+ minutes.** It's downloading ~10 GB.
- **A Chrome window opens on its own.** That's it working. Don't close it mid-application.
- **Jobs sitting in "Needs your decision".** Deliberate — it wasn't confident enough to proceed
  alone.
- **"Windows protected your PC" on install.** Covered above.
- **Scores that look harsh.** It's comparing against what you entered. Thin work history in →
  low scores out.

---

## 5. Uninstalling

Add/Remove Programs → **Auto Applier** → Uninstall. **Your data is left alone** — the jobs it
found, your details, and the documents it wrote stay in your user folder so a reinstall picks up
where you left off. Delete that folder yourself if you want it properly gone.

---

## What I most want to hear about

1. **Anything confusing in the wizard.** If a step didn't make sense, that's a bug in my
   writing, not in you.
2. **A form it filled wrong.** Especially a *wrong answer* rather than a blank one — those are
   the ones that matter most.
3. **Anything that made you nervous.** If it did something that felt like too much, I want to
   know before anyone applies to a job they care about.
