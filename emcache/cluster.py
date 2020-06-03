import logging
from typing import Dict, List, Optional, Sequence

from ._cython import cyemcache
from .base import ClusterEvents
from .client_errors import ClusterNoAvailableNodes
from .node import MemcachedHostAddress, Node

logger = logging.getLogger(__name__)


class Cluster:
    """Cluster class is used for keeping all of the nodes together, and
    for providing any interface that needs to have visibility of all of
    those nodes that belong to a cluster.

    As an example, the node selection for a sepcific key, is being provided
    by the Cluster class.
    """

    _cluster_events: Optional[ClusterEvents]
    _purge_unhealthy_nodes: bool
    _healthy_nodes: List[Node]
    _unhelathy_nodes: List[Node]
    _rdz_nodes: List[cyemcache.RendezvousNode]

    def __init__(
        self,
        memcached_hosts_address: Sequence[MemcachedHostAddress],
        max_connections: int,
        purge_unused_connections_after: Optional[float],
        connection_timeout: Optional[float],
        cluster_events: Optional[ClusterEvents],
        purge_unhealthy_nodes: bool,
    ) -> None:

        if not memcached_hosts_address:
            raise ValueError("Nodes can't be an empty list, at least one node has to be provided")

        self._cluster_events = cluster_events
        self._purge_unhealthy_nodes = purge_unhealthy_nodes

        # Create nodes and configure them to be used by the Rendezvous
        # hashing. By default all are placed under the healthy list, if later
        # on they report that are unhealhty and `purge_unhealthy_nodes` has been
        # configured they will be removed.
        self._unhealthy_nodes = []
        self._healthy_nodes = [
            Node(
                memcached_host_address,
                max_connections,
                purge_unused_connections_after,
                connection_timeout,
                self._on_node_healthy_status_change_cb,
            )
            for memcached_host_address in memcached_hosts_address
        ]
        self._build_rdz_nodes()
        logger.debug(f"Cluster configured with {len(self._healthy_nodes)} nodes")

    def _build_rdz_nodes(self):
        """ Builds the list of rdz nodes using the nodes that claim to be healhty."""
        if self._purge_unhealthy_nodes:
            nodes = self._healthy_nodes
        else:
            nodes = self._healthy_nodes + self._unhealthy_nodes

        logger.info(f"{self} Nodes used for sending traffic: {nodes}")

        self._rdz_nodes = [cyemcache.RendezvousNode(node.host, node.port, node) for node in nodes]

    def _on_node_healthy_status_change_cb(self, node: Node, healthy: bool):
        """ Called by the node for telling that the healthy status has changed. """
        if healthy:
            assert node in self._unhealthy_nodes, "Node was not tracked by the cluster as unhealthy!"
            self._unhealthy_nodes.remove(node)
            self._healthy_nodes.append(node)
            logger.info(f"{self} Node {node} reports a healthy status")
        else:
            assert node in self._healthy_nodes, "Node was not tracked by the cluster as healthy!"
            self._healthy_nodes.remove(node)
            self._unhealthy_nodes.append(node)
            logger.warning(f"{self} Node {node} reports an unhealthy status")

        self._build_rdz_nodes()

        # trigger cluster events if they were provided
        if self._cluster_events:
            if healthy:
                self._cluster_events.on_node_healthy(node.memcached_host_address)
            else:
                self._cluster_events.on_node_unhealthy(node.memcached_host_address)

    def pick_node(self, key: bytes) -> Node:
        """Return the most appropiate node for the given key.

        Node selected will be resolved by the Rendezvous hashing
        algorithm, which will be idempotent when the cluster nodes
        do not change.
        """
        if len(self._rdz_nodes) == 0:
            raise ClusterNoAvailableNodes()

        return cyemcache.node_selection(key, self._rdz_nodes)

    def pick_nodes(self, keys: bytes) -> Dict[Node, List[bytes]]:
        """Return the most appropiate nodes for the given keys.

        Return value is a dictionary where nodes stand for keys
        and values are the list of keys that would need to be used
        for that node.

        Nodes selected will be resolved by the Rendezvous hashing
        algorithm, which will be idempotent when the cluster nodes
        do not change.
        """
        if len(self._rdz_nodes) == 0:
            raise ClusterNoAvailableNodes()

        return cyemcache.nodes_selection(keys, self._rdz_nodes)
