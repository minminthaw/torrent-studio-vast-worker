import os

from vastai import BenchmarkConfig, HandlerConfig, LogActionConfig, Worker, WorkerConfig


def workload(payload: dict) -> float:
    return max(1.0, float(payload.get("duration", 1.0)))


handlers = [
    HandlerConfig(
        route="/benchmark",
        allow_parallel_requests=True,
        workload_calculator=lambda payload: 1.0,
        benchmark_config=BenchmarkConfig(dataset=[{}], runs=1, concurrency=1),
    ),
    HandlerConfig(route="/start_task", allow_parallel_requests=True, workload_calculator=workload),
    HandlerConfig(route="/status", allow_parallel_requests=True, workload_calculator=lambda payload: 1.0),
    HandlerConfig(route="/download", allow_parallel_requests=True, workload_calculator=lambda payload: 1.0),
    HandlerConfig(route="/cleanup", allow_parallel_requests=True, workload_calculator=lambda payload: 1.0),
    HandlerConfig(route="/cancel_task", allow_parallel_requests=True, workload_calculator=lambda payload: 1.0),
]

config = WorkerConfig(
    model_server_url="http://127.0.0.1",
    model_server_port=int(os.environ.get("TORRENT_MODEL_PORT", "8080")),
    model_log_file=os.environ.get("TORRENT_MODEL_LOG", "/var/log/torrent-model.log"),
    model_healthcheck_url="/health",
    max_sessions=1,
    handlers=handlers,
    log_action_config=LogActionConfig(
        on_load=["Torrent encoder ready"],
        on_error=["Torrent encoder fatal", "Traceback (most recent call last):"],
    ),
)

Worker(config).run()
