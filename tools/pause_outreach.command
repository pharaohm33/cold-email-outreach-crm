#!/bin/bash
# Master stop: the helper finishes the ONE unit in hand (a form message, an email draft, an upload, a video) and then nothing runs and nothing is sent until you resume.
# Optional: bash tools/pause_outreach.command 90   (resumes by itself after 90 minutes)
curl -s -X POST http://127.0.0.1:8765/loop/pause -d "{\"minutes\": ${1:-0}}" && echo && echo "Paused. Run tools/resume_outreach.command to carry on."
