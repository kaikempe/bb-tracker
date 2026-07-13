<div align="center">
  <img src="app_icon.png" width="128" alt="BB Tracker icon">
  <h1>BB Tracker</h1>
  <p><strong>Blackboard, in your Mac's menu bar.</strong></p>
  <p>Grades, absences, deadlines and your GPA, without ever opening Blackboard.</p>
  <p>
    <a href="https://bblivetracker.netlify.app">Download</a>
    &nbsp;·&nbsp;
    <a href="#how-it-works">How it works</a>
    &nbsp;·&nbsp;
    <a href="#privacy">Privacy</a>
  </p>
</div>

---

## Why I built this

I got tired of logging into Blackboard five times a day just to check if I could afford to skip one more class. So I built a little menu bar app for myself that kept track of it. Then classmates saw it on my screen and started asking where they could get it.

That's when it turned into a real project. I've been building it out ever since: proper grade tracking, a GPA that actually matches the transcript, deadlines, Pearson MyLab, all of it. Right now I'm polishing everything for the official launch in September, ready for the start of the semester.

## At a glance

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="screenshots/menubar-dark.png">
  <img src="screenshots/menubar-light.png" alt="BB Tracker open in the menu bar, every course with its grade and absences left" width="720">
</picture>

A small indicator lives in your menu bar (your GPA can sit right next to the clock). Open it and you see every course with the grade you have so far and how many absences you have left before the 20% cap kicks in, riskiest course first.

## What it does

- **Attendance, computed.** It pulls your present / late / absent counts from Blackboard and tells you exactly how many classes you can still miss per course before crossing the 80% rule. No more counting on your fingers.
- **Grades that match your transcript.** It shows Blackboard's own calculated course total, the same number that ends up on your transcript. If a course doesn't have one, it parses the syllabus weights and does the math itself.
- **A real GPA.** Computed the way the registrar computes it: language and lab courses left out, retakes replacing fails, credits weighting each course.
- **What-if projector.** Type a score for the final and watch your course grade move. Or flip it around and ask what you need to pass.
- **Every deadline in one list.** Blackboard and Pearson MyLab due dates together in one To-Dos view. Click one and you land on the actual assignment.
- **Pearson MyLab built in.** If a course runs on Pearson, those assignments and scores show up next to your Blackboard ones.
- **Announcements that know when to bother you.** A new announcement mentioning a deadline, exam or something mandatory triggers a macOS notification. The rest stay quiet.
- **Fully automatic.** It refreshes every two hours in the background. No tabs to keep open, nothing to maintain.

## Screenshots

**Dashboard.** Every course, its attendance budget, and a term grade projector:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="screenshots/dashboard-dark.png">
  <img src="screenshots/dashboard-light.png" alt="Dashboard with course cards, attendance, and term grade projector">
</picture>

**To-Dos.** Every deadline from Blackboard and Pearson in one list:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="screenshots/todos-dark.png">
  <img src="screenshots/todos-light.png" alt="To-Dos view with deadlines from Blackboard and Pearson">
</picture>

**Transcript.** Your GPA, computed the way the registrar computes it:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="screenshots/transcript-dark.png">
  <img src="screenshots/transcript-light.png" alt="Transcript view with registrar-style GPA">
</picture>

<sub>The app follows your system appearance. Every screen has a light and a dark version.</sub>

## How it works

You log in with your IE Microsoft account once. BB Tracker keeps that login session and uses it to sync your own course data straight from Blackboard's API in the background. No bundled browser, no servers in between, everything stays on your Mac.

When a sync finishes, the result lives in `~/Library/Application Support/BBTracker/` as plain JSON, and the menu bar and dashboard read from that.

## Privacy

I built this for myself first, so it works the way I'd want any app to work with my own grades:

- **Local-only.** Your Blackboard cookies, grades and attendance never leave your Mac.
- **No analytics.** No telemetry, no crash uploads, nothing phoning home.
- **One exception.** The only network call outside your own Blackboard and Pearson accounts is a weekly license check with Lemon Squeezy. That's it.

## Pricing

7-day free trial, no card needed. After that it's a small one-time or yearly fee. Current pricing is on the [download page](https://bblivetracker.netlify.app).

## System requirements

- macOS 12 (Monterey) or later, Apple Silicon or Intel
- An IE University Blackboard account

## Support

Questions, bugs, feature requests: [bblivetracker@gmail.com](mailto:bblivetracker@gmail.com). I read everything.

---

<sub>BB Tracker is an independent project and is not affiliated with, endorsed by, or sponsored by IE University or Blackboard Inc. *Blackboard* is a trademark of Blackboard Inc. *IE University* is a trademark of IE University.</sub>

<sub>Source code is proprietary. This repository hosts the public landing material only.</sub>
