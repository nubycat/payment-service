# Payment Service

> **Work in progress**

A payment operation service designed to coordinate reliable payments with an external provider.

## Stack

Python 3.14, FastAPI, Pydantic Settings, SQLAlchemy asyncio, PostgreSQL, and Uvicorn.

## Current state

- FastAPI application skeleton;
- `GET /health` endpoint;
- environment-based configuration;
- structured JSON logging;
- SQLAlchemy declarative base, async engine, and session factory;
- enum statuses for operations, submission intents, and receipts.

The payment workflow, persistence models, migrations, and provider integration are still under development.
