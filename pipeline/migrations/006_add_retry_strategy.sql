-- Migration: Add retry_strategy to jobs and template_jobs

-- Add retry_strategy to jobs table
ALTER TABLE jobs ADD COLUMN retry_strategy JSON;

-- Add retry_strategy to template_jobs table
ALTER TABLE template_jobs ADD COLUMN retry_strategy JSON;

-- Add original_prompt to jobs table to preserve prompt before retry augmentation
ALTER TABLE jobs ADD COLUMN original_prompt TEXT;

-- Backfill original_prompt with current prompt value for existing jobs
UPDATE jobs SET original_prompt = prompt WHERE original_prompt IS NULL;
