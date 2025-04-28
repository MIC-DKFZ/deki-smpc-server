import os
from typing import List, Tuple

import flwr as fl
from flwr.common import Metrics
from flwr.server.client_manager import SimpleClientManager
from strategies.aotp_strategy_fed_avg import AOTPStrategy

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
EPOCHS = int(os.environ.get("EPOCHS", 1))
NUM_CLIENTS = int(os.environ.get("NUM_CLIENTS", 10))


# Define metric aggregation function
def weighted_average(metrics: List[Tuple[int, Metrics]]) -> Metrics:
    # Multiply accuracy of each client by number of examples used
    accuracies = [num_examples * m["accuracy"] for num_examples, m in metrics]
    examples = [num_examples for num_examples, _ in metrics]

    # Aggregate and return custom metric (weighted average)
    return {"accuracy": sum(accuracies) / sum(examples)}


strategy = AOTPStrategy(
    evaluate_metrics_aggregation_fn=weighted_average,
    min_available_clients=NUM_CLIENTS,
    min_fit_clients=NUM_CLIENTS,
)

# Define ClientManager
client_manager = SimpleClientManager()

# Start Flower server
fl.server.start_server(
    server_address=f"{HOST}:{PORT}",
    config=fl.server.ServerConfig(num_rounds=EPOCHS),
    strategy=strategy,
    client_manager=client_manager,
    grpc_max_message_length=-1,
)
