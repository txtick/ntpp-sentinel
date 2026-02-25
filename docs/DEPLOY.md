# Sentinel Deployment Guide

This document defines the standard deployment workflow for Sentinel.

Production path:
Laptop → GitHub → Droplet → Docker restart

---

# 1) Deployment Model

Sentinel is deployed via:

- GitHub as source of truth
- Git pull on production droplet
- Docker Compose rebuild + restart

Never develop directly on the droplet.

---

# 2) One-Time Production Setup

On droplet:

    cd /opt
    git clone https://github.com/txtick/ntpp-sentinel.git
    cd ntpp-sentinel

Set production branch:

    git checkout main

Ensure .env exists:

    nano .env

Ensure Docker works:

    docker compose up -d --build

---

# 3) Standard Deployment Flow

## Step 1 – Develop Locally

On laptop:

    git checkout -b dev
    <make changes>
    git add -A
    git commit -m "Describe change"
    git push -u origin dev

## Step 2 – Merge to main

    git checkout main
    git merge dev
    git push origin main

## Step 3 – Deploy to Production

SSH to droplet:

    ssh sentinel

Pull and restart:

    cd /opt/ntpp-sentinel
    git fetch --all --tags
    git checkout main
    git pull --ff-only
    docker compose down
    docker compose up -d --build

Verify:

    docker compose logs -f --tail=100

---

# 4) One-Line Remote Deploy (Optional)

From laptop:

    ssh kevin@sentinel 'cd /opt/ntpp-sentinel && git checkout main && git pull --ff-only && docker compose up -d --build'

---

# 5) Rollback Procedure

List commits:

    git log --oneline

Checkout previous commit:

    git checkout <commit_hash>

Restart Docker:

    docker compose down
    docker compose up -d --build

If stable:

    git checkout main
    git reset --hard <stable_commit>
    git push --force origin main

---

# 6) Tagging Releases

Tag:

    git tag v0.1.1
    git push origin refs/tags/v0.1.1

Push all tags:

    git push origin --tags

---

# 7) Production Health Checks

Test API:

    curl https://sentinel.northtexaspoolpros.com/health

Test summary dry run:

    curl -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1" \
    -H "X-NTPP-Secret: $WEBHOOK_SECRET"

Check logs:

    docker compose logs -f --tail=100