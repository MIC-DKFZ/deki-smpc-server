from functools import reduce
from logging import WARNING
from typing import Dict, List, Optional, Tuple, Union

from flwr.common import (
    FitRes,
    MetricsAggregationFn,
    NDArrays,
    Parameters,
    Scalar,
    ndarrays_to_parameters,
    parameters_to_ndarrays,
)
from flwr.common.logger import log
from flwr.server.client_proxy import ClientProxy
from flwr.server.strategy import FedAvg


def aggregate(results: List[Tuple[NDArrays, int]]) -> NDArrays:
    """Compute sum of weights."""

    # Unzip results
    weights, _ = zip(*results)

    # Compute weighted sum of weights
    weights_sum = reduce(lambda a, b: [x + y for x, y in zip(a, b)], weights)

    # Return sum of weights
    return weights_sum


class AOTPStrategy(FedAvg):

    def __init__(
        self,
        evaluate_metrics_aggregation_fn,
        min_available_clients,
        min_fit_clients,
    ):
        super().__init__(
            evaluate_metrics_aggregation_fn=evaluate_metrics_aggregation_fn,
            min_available_clients=min_available_clients,
            min_fit_clients=min_fit_clients,
        )

    def aggregate_fit(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, FitRes]],
        failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]],
    ) -> Tuple[Optional[Parameters], Dict[str, Scalar]]:
        """Aggregate fit results using weighted average."""

        if not results:
            return None, {}
        # Do not aggregate if there are failures and failures are not accepted
        if not self.accept_failures and failures:
            return None, {}

        # Convert results
        weights_results = [
            (parameters_to_ndarrays(fit_res.parameters), fit_res.num_examples)
            for _, fit_res in results
        ]
        parameters_aggregated = ndarrays_to_parameters(aggregate(weights_results))

        # Aggregate custom metrics if aggregation fn was provided
        metrics_aggregated = {}
        if self.fit_metrics_aggregation_fn:
            fit_metrics = [(res.num_examples, res.metrics) for _, res in results]
            metrics_aggregated = self.fit_metrics_aggregation_fn(fit_metrics)
        elif server_round == 1:  # Only log this warning once
            log(WARNING, "No fit_metrics_aggregation_fn provided")

        return parameters_aggregated, metrics_aggregated

    def evaluate(
        self, server_round: int, parameters: Parameters
    ) -> Optional[Tuple[float, Dict[str, Scalar]]]:

        return None
