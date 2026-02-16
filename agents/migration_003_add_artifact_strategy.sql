-- Migration: Add artifact_strategy support to jobs and templates

-- Add to template_jobs
ALTER TABLE template_jobs ADD COLUMN artifact_strategy JSON;

-- Add to jobs
ALTER TABLE jobs ADD COLUMN artifact_strategy JSON;
