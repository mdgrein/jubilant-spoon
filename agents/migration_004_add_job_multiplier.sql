-- Migration: Add job_multiplier support for dynamic job spawning

ALTER TABLE template_jobs ADD COLUMN job_multiplier JSON;
