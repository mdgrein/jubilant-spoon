-- Migration: Add job output storage
-- Jobs need to store their stdout/stderr for debugging and display

ALTER TABLE jobs ADD COLUMN job_output TEXT;
