-- Migration: Add retry support to jobs table
-- Run this with: python -c "import sqlite3; conn = sqlite3.connect('clowder.db'); conn.executescript(open('agents/migration_001_add_retry_columns.sql').read()); conn.commit(); conn.close()"

-- Add retry tracking columns
ALTER TABLE jobs ADD COLUMN retry_count INTEGER DEFAULT 0;
ALTER TABLE jobs ADD COLUMN max_retries INTEGER DEFAULT 2;

-- Update existing jobs to have default values
UPDATE jobs SET retry_count = 0 WHERE retry_count IS NULL;
UPDATE jobs SET max_retries = 2 WHERE max_retries IS NULL;
