#!/usr/bin/env python3
"""Lease-based, research-only Subfortress task worker.

At-least-once execution: interrupted model calls may be repeated. No external
side-effecting tools are registered. Unknown outcomes must not be auto-retried.
"""
import asyncio
import configparser
import json
import os
import signal
import socket
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

cfg = configparser.ConfigParser(interpolation=None)
if not cfg.read(os.environ.get('SUBFORTRESS_INI', '/etc/subfortress/agent.ini')):
    raise RuntimeError('Missing INI configuration')
db = cfg['database']
worker = cfg['worker']
NAME = os.environ.get('HOSTNAME', socket.gethostname())
LEASE = worker.getint('lease_seconds', fallback=120)
HEARTBEAT = worker.getint('heartbeat_seconds', fallback=20)
POLL = worker.getfloat('poll_seconds', fallback=5)
assert LEASE >= 30 and 0 < HEARTBEAT < LEASE / 2
stop = threading.Event()


def connect():
    return psycopg.connect(
        host=db['host'], port=db.getint('port'), dbname=db['name'],
        user=db['user'], password=os.environ['SUBFORTRESS_DB_PASSWORD'],
        sslmode=db['sslmode'], sslrootcert=db['sslrootcert'],
        connect_timeout=8, row_factory=dict_row,
        options='-c search_path=agents,public',
    )


def claim():
    """Atomic claim; reclaim only expired leases; retain original agent assignment."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute('''
            SELECT t.id, t.title, t.description, t.status, t.checkpoint
            FROM agents.tasks t
            WHERE (t.status = 'pending' OR
                   (t.status = 'running' AND t.lease_expires_at < now()))
              AND NOT EXISTS (
                SELECT 1 FROM agents.operations o
                WHERE o.task_id = t.id AND o.status IN ('running', 'unknown')
              )
            ORDER BY t.created_at, t.id
            FOR UPDATE OF t SKIP LOCKED LIMIT 1
        ''')
        task = cur.fetchone()
        if not task:
            return None
        if task['status'] == 'running':
            cur.execute('''UPDATE agents.task_executions
                SET status='interrupted', finished_at=now(),
                    error_message='Lease expired; recovered by another worker'
                WHERE task_id=%s AND status='running' ''', (task['id'],))
        cur.execute('''UPDATE agents.tasks
            SET status='running', lease_expires_at=now()+(%s * interval '1 second'),
                updated_at=now()
            WHERE id=%s''', (LEASE, task['id']))
        cur.execute('''INSERT INTO agents.task_executions
            (task_id,agent_id,status,worker_name,checkpoint)
            SELECT id,assigned_agent,'running',%s,checkpoint
            FROM agents.tasks WHERE id=%s RETURNING id''', (NAME, task['id']))
        task['execution_id'] = cur.fetchone()['id']
        conn.commit()
        return task


def heartbeat(task_id, execution_id, halted):
    while not halted.wait(HEARTBEAT):
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute('''UPDATE agents.tasks SET
                    lease_expires_at=now()+(%s * interval '1 second'),
                    updated_at=now()
                    WHERE id=%s AND status='running'
                    AND EXISTS (SELECT 1 FROM agents.task_executions
                                WHERE id=%s AND status='running')''',
                            (LEASE, task_id, execution_id))
                if cur.rowcount != 1:
                    halted.set()
                    return
        except Exception as exc:
            print(f'heartbeat failure: {exc}', flush=True)
            # Do not execute further side effects: this worker has none.


def finish(task, result=None, error=None):
    with connect() as conn, conn.cursor() as cur:
        status = 'failed' if error else 'completed'
        cur.execute('''UPDATE agents.task_executions
            SET status=%s, finished_at=now(), error_message=%s,
                checkpoint=%s::jsonb
            WHERE id=%s AND status='running' ''',
            (status, error, json.dumps({'phase': 'finished'}), task['execution_id']))
        if cur.rowcount != 1:
            raise RuntimeError('Execution ownership lost; refusing to finalize task')
        cur.execute('''UPDATE agents.tasks SET status=%s,
            result=%s::jsonb, checkpoint=%s::jsonb,
            lease_expires_at=NULL, completed_at=CASE WHEN %s='completed' THEN now() ELSE NULL END,
            updated_at=now()
            WHERE id=%s AND status='running' ''',
            (status, json.dumps({'answer': result} if not error else {'error': error}),
             json.dumps({'phase': 'finished'}), status, task['id']))
        if cur.rowcount != 1:
            raise RuntimeError('Task ownership lost; refusing to finalize')


async def research(task):
    provider = OpenAIProvider(base_url=cfg['model']['base_url'], api_key=cfg['model']['api_key'])
    model = OpenAIChatModel(cfg['model']['name'], provider=provider)
    agent = Agent(model, instructions=(
        'You are a research-only task worker. Produce a useful written response. '
        'You have no tools, browsing, shell, or ability to change infrastructure. '
        'Do not claim to have performed actions or verified external facts.'))
    prompt = f"Task: {task['title']}\n\nDescription: {task['description'] or ''}"
    output = await agent.run(prompt)
    return str(output.output)


def execute(task):
    print(f"claimed {task['id']} execution {task['execution_id']}", flush=True)
    halted = threading.Event()
    thread = threading.Thread(target=heartbeat,
        args=(task['id'], task['execution_id'], halted), daemon=True)
    thread.start()
    try:
        answer = asyncio.run(research(task))
        finish(task, result=answer)
        print(f"completed {task['id']}", flush=True)
    except Exception as exc:
        print(f"failed {task['id']}: {exc}", flush=True)
        try:
            finish(task, error=f'{type(exc).__name__}: {exc}'[:2000])
        except Exception as db_exc:
            print(f'Could not record failure; lease will expire: {db_exc}', flush=True)
    finally:
        halted.set()
        thread.join(timeout=2)


def main():
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(f'worker {NAME} ready (lease={LEASE}s heartbeat={HEARTBEAT}s)', flush=True)
    while not stop.is_set():
        try:
            task = claim()
            if task:
                execute(task)
            else:
                stop.wait(POLL)
        except Exception as exc:
            print(f'worker polling error: {type(exc).__name__}: {exc}', flush=True)
            stop.wait(POLL)


if __name__ == '__main__':
    main()
