import threading
import time

from collections_helper.collections_spec_constants import MetaCrudParams
from bucket_collections.collections_base import CollectionBase
from membase.api.rest_client import RestConnection
from platform_utils.remote.remote_util import RemoteMachineShellConnection
from sdk_exceptions import SDKException


class CollectionsQuorumLoss(CollectionBase):
    def setUp(self):
        super(CollectionsQuorumLoss, self).setUp()
        self.failover_action = self.input.param("failover_action", None)
        self.num_node_failures = self.input.param("num_node_failures", 3)
        self.failover_orchestrator = self.input.param("failover_orchestrator", False)
        self.nodes_in_cluster = self.cluster.servers[:self.nodes_init]
        self.create_zones = self.input.param("create_zones", False)

        self.data_loading_thread = None
        self.data_load_flag = False
        self.data_load_exception = None # Object variable to assign data load thread's exception

    def tearDown(self):
        if self.failover_action:
            self.custom_remove_failure()
        if self.data_loading_thread:
            # stop data loading before tearDown if its still running
            self.data_load_flag = False
            self.data_loading_thread.join()
            self.data_loading_thread = None
        super(CollectionsQuorumLoss, self).tearDown()

    def get_failover_count(self):
        rest = RestConnection(self.cluster.master)
        cluster_status = rest.cluster_status()
        failover_count = 0
        # check for inactiveFailed
        for node in cluster_status['nodes']:
            if node['clusterMembership'] == "inactiveFailed":
                failover_count += 1
        return failover_count

    def wait_for_failover_or_assert(self, expected_failover_count, timeout=180):
        time_start = time.time()
        time_max_end = time_start + timeout
        actual_failover_count = 0
        while time.time() < time_max_end:
            actual_failover_count = self.get_failover_count()
            if actual_failover_count == expected_failover_count:
                break
            time.sleep(20)
        time_end = time.time()
        if actual_failover_count != expected_failover_count:
            self.log.info(self.rest.print_UI_logs())
        self.assertTrue(actual_failover_count == expected_failover_count,
                        "{0} nodes failed over, expected : {1}"
                        .format(actual_failover_count,
                                expected_failover_count))
        self.log.info("{0} nodes failed over as expected in {1} seconds"
                      .format(actual_failover_count, time_end - time_start))

    def wait_for_rebalance_to_complete(self, task):
        self.task.jython_task_manager.get_task_result(task)
        self.assertTrue(task.result, "Rebalance Failed")

    @staticmethod
    def get_common_spec():
        spec = {
            # Scope/Collection ops params
            MetaCrudParams.COLLECTIONS_TO_FLUSH: 0,
            MetaCrudParams.COLLECTIONS_TO_DROP: 25,

            MetaCrudParams.SCOPES_TO_DROP: 0,
            MetaCrudParams.SCOPES_TO_ADD_PER_BUCKET: 0,
            MetaCrudParams.COLLECTIONS_TO_ADD_FOR_NEW_SCOPES: 0,

            MetaCrudParams.COLLECTIONS_TO_ADD_PER_BUCKET: 0,

            # Only dropped scope/collection will be created.
            # While scope recreated all prev collection will also be created
            # In both the collection creation case, previous maxTTL value of
            # individual collection is considered
            MetaCrudParams.SCOPES_TO_RECREATE: 0,
            MetaCrudParams.COLLECTIONS_TO_RECREATE: 25,

            # Applies only for the above listed scope/collection operations
            MetaCrudParams.BUCKET_CONSIDERED_FOR_OPS: "all",
            MetaCrudParams.SCOPES_CONSIDERED_FOR_OPS: "all",
            MetaCrudParams.COLLECTIONS_CONSIDERED_FOR_OPS: "all",

            # Doc loading params
            "doc_crud": {

                MetaCrudParams.DocCrud.NUM_ITEMS_FOR_NEW_COLLECTIONS: 500,

                MetaCrudParams.DocCrud.COMMON_DOC_KEY: "test_collections",
                MetaCrudParams.DocCrud.CREATE_PERCENTAGE_PER_COLLECTION: 0,
                MetaCrudParams.DocCrud.READ_PERCENTAGE_PER_COLLECTION: 0,
                MetaCrudParams.DocCrud.UPDATE_PERCENTAGE_PER_COLLECTION: 10,
                MetaCrudParams.DocCrud.REPLACE_PERCENTAGE_PER_COLLECTION: 0,
                MetaCrudParams.DocCrud.DELETE_PERCENTAGE_PER_COLLECTION: 10,
            },
        }
        return spec

    def set_retry_exceptions(self, doc_loading_spec):
        retry_exceptions = list()
        retry_exceptions.append(SDKException.AmbiguousTimeoutException)
        retry_exceptions.append(SDKException.TimeoutException)
        retry_exceptions.append(SDKException.RequestCanceledException)
        retry_exceptions.append(SDKException.DocumentNotFoundException)
        if self.durability_level:
            retry_exceptions.append(SDKException.DurabilityAmbiguousException)
            retry_exceptions.append(SDKException.DurabilityImpossibleException)
        doc_loading_spec[MetaCrudParams.RETRY_EXCEPTIONS] = retry_exceptions

    @staticmethod
    def set_ignore_exceptions(doc_loading_spec):
        ignore_exceptions = list()
        ignore_exceptions.append(SDKException.DocumentNotFoundException)
        doc_loading_spec[MetaCrudParams.IGNORE_EXCEPTIONS] = ignore_exceptions

    def data_load(self):
        """
        Continuous data load
        """
        while self.data_load_flag:
            doc_loading_spec = self.get_common_spec()
            self.set_retry_exceptions(doc_loading_spec)
            self.set_ignore_exceptions(doc_loading_spec)
            try:
                tasks = self.bucket_util.run_scenario_from_spec(self.task,
                                                                self.cluster,
                                                                self.bucket_util.buckets,
                                                                doc_loading_spec,
                                                                mutation_num=0,
                                                                async_load=False,
                                                                batch_size=self.batch_size)
                if tasks.result is False:
                    raise Exception("subsequent doc loading task failed")
            except Exception as e:
                self.data_load_exception = e
                raise

    def servers_to_fail(self):
        """
        Select the nodes to be failed in the tests, and
        update the master, rest object accordingly
        :return: nodes to fail
        """
        if self.failover_orchestrator:
            servers_to_fail = list()
            servers_to_fail.extend(self.nodes_in_cluster[0:self.num_node_failures])
            self.cluster.master = self.master = self.orchestrator = self.cluster.servers[self.num_node_failures]
            self.log.info("changing master to {0}".format(self.cluster.master))
            # Swap first node and last node in the list of current_servers.
            # Because first node is going to get failed - to avoid rest connection
            # to first node in rebalance/failover task
            first_node = self.nodes_in_cluster[0]
            self.nodes_in_cluster[0] = self.nodes_in_cluster[-1]
            self.nodes_in_cluster[-1] = first_node
            self.log.info("also modifying self.nodes.in.cluster to {0} ".format(self.nodes_in_cluster))
        else:
            servers_to_fail = self.nodes_in_cluster[1:self.num_node_failures + 1]
        self.rest = RestConnection(self.cluster.master)
        return servers_to_fail

    def custom_induce_failure(self):
        """
        Induce failure on nodes
        """
        for node in self.server_to_fail:
            if self.failover_action == "stop_server":
                self.cluster_util.stop_server(node)
            elif self.failover_action == "firewall":
                self.cluster_util.start_firewall_on_node(node)
            elif self.failover_action == "stop_memcached":
                self.cluster_util.stop_memcached_on_node(node)
            elif self.failover_action == "kill_erlang":
                remote = RemoteMachineShellConnection(node)
                remote.kill_erlang()
                remote.disconnect()

    def custom_remove_failure(self):
        """
        Remove failure
        """
        for node in self.server_to_fail:
            if self.failover_action == "stop_server":
                self.cluster_util.start_server(node)
            elif self.failover_action == "firewall":
                self.cluster_util.stop_firewall_on_node(node)
            elif self.failover_action == "stop_memcached":
                self.cluster_util.start_memcached_on_node(node)
            elif self.failover_action == "kill_erlang":
                self.cluster_util.stop_server(node)
                self.cluster_util.start_server(node)

    def shuffle_nodes_between_two_zones(self):
        """
        Creates 'Group 2' zone and shuffles nodes between
        Group 1 and Group 2 in an alternate manner ie;
        1st node in Group 1, 2nd node in Group 2, 3rd node in Group 1 and so on
        and finally rebalances the resulting cluster
        :return nodes of 2nd zone
        """
        serverinfo = self.cluster.master
        rest = RestConnection(serverinfo)
        zones = ["Group 1", "Group 2"]
        rest.add_zone("Group 2")
        nodes_in_zone = {"Group 1": [serverinfo.ip], "Group 2": []}
        second_zone_servers = list()  # Keep track of second zone's nodes
        # Divide the nodes between zones.
        for i in range(1, len(self.nodes_in_cluster)):
            server_group = i % 2
            nodes_in_zone[zones[server_group]].append(self.nodes_in_cluster[i].ip)
            if zones[server_group] == "Group 2":
                second_zone_servers.append(self.nodes_in_cluster[i])
        # Shuffle the nodes
        node_in_zone = list(set(nodes_in_zone[zones[1]]) -
                            set([node for node in rest.get_nodes_in_zone(zones[1])]))
        rest.shuffle_nodes_in_zones(node_in_zone, zones[0], zones[1])
        self.task.rebalance(self.nodes_in_cluster, [], [])
        return second_zone_servers

    def test_quorum_loss_failover(self):
        """
        With constant parallel data load(on docs and collections) do:
        0. Pick majority nodes for failover
        1. Induce failure on step0 nodes and fail over them at once
            OR
            manually failover without inducing failure
        2. Rebalance-out
        3. Remove failures if you had added them
        4. Add rebalanced out nodes back again
        """
        if self.create_zones:
            self.server_to_fail = self.shuffle_nodes_between_two_zones()
        else:
            self.server_to_fail = self.servers_to_fail()

        self.data_load_flag = True
        self.data_loading_thread = threading.Thread(target=self.data_load)
        self.data_loading_thread.start()

        if self.failover_action:
            self.log.info("Inducing failure {0} on nodes: {1}".
                          format(self.failover_action, self.server_to_fail))
            self.custom_induce_failure()
            self.sleep(60, "Wait before failing over")

        self.log.info("Failing over nodes explicitly {0}".format(self.server_to_fail))
        _ = self.task.failover(self.nodes_in_cluster, failover_nodes=self.server_to_fail,
                               graceful=False, wait_for_pending=120,
                               allow_unsafe=True,
                               all_at_once=True)
        self.wait_for_failover_or_assert(len(self.server_to_fail))

        self.log.info("Rebalancing out nodes {0}".format(self.server_to_fail))
        rebalance_task = self.task.async_rebalance(self.nodes_in_cluster, [], [],
                                                   retry_get_process_num=100)
        self.wait_for_rebalance_to_complete(rebalance_task)
        if self.failover_action:
            self.custom_remove_failure()
            self.sleep(60, "wait after removing failure")

        self.log.info("Adding back nodes which were failed and rebalanced out".
                      format(self.server_to_fail))
        rebalance_task = self.task.async_rebalance(self.nodes_in_cluster, self.server_to_fail, [],
                                                   retry_get_process_num=100)
        self.wait_for_rebalance_to_complete(rebalance_task)

        self.data_load_flag = False
        self.data_loading_thread.join()
        if self.data_load_exception:
            self.log.error("Caught exception from data load thread")
            self.fail(self.data_load_exception)

    def test_quorum_loss_failover_in_steps(self):
        """
        With constant parallel data load(on docs and collections) do:
        0. Pick majority nodes for failover
        1. Induce failure on step0 nodes and fail over them in two steps
        2. Rebalance
        3. Remove failures if you had added them
        4. Add rebalanced out nodes back again
        """

        if self.create_zones:
            self.server_to_fail = self.shuffle_nodes_between_two_zones()
        else:
            self.server_to_fail = self.servers_to_fail()

        self.data_load_flag = True
        self.data_loading_thread = threading.Thread(target=self.data_load)
        self.data_loading_thread.start()

        if self.failover_action:
            self.log.info("Inducing failure {0} on nodes: {1}".
                          format(self.failover_action, self.server_to_fail))
            self.custom_induce_failure()
            self.sleep(60, "Wait before failing over")

        failover_step_nodes = [self.server_to_fail[:-1], self.server_to_fail[-1]] # 2 steps
        for failover_nodes in failover_step_nodes:
            self.log.info("Failing over nodes explicitly {0}".format(failover_nodes))
            _ = self.task.failover(self.nodes_in_cluster, failover_nodes=failover_nodes,
                                   graceful=False, wait_for_pending=120,
                                   allow_unsafe=True,
                                   all_at_once=True)
            self.wait_for_failover_or_assert(len(failover_nodes))

        self.log.info("Rebalancing out nodes {0}".format(self.server_to_fail))
        rebalance_task = self.task.async_rebalance(self.nodes_in_cluster, [], [],
                                                   retry_get_process_num=100)
        self.wait_for_rebalance_to_complete(rebalance_task)
        if self.failover_action:
            self.custom_remove_failure()
            self.sleep(60, "wait after removing failure")

        self.log.info("Adding back nodes which were failed and rebalanced out".
                      format(self.server_to_fail))
        rebalance_task = self.task.async_rebalance(self.nodes_in_cluster, self.server_to_fail, [],
                                                   retry_get_process_num=100)
        self.wait_for_rebalance_to_complete(rebalance_task)

        self.data_load_flag = False
        self.data_loading_thread.join()
        if self.data_load_exception:
            self.log.error("Caught exception from data load thread")
            self.fail(self.data_load_exception)

    def test_quorum_loss_failover_more_than_failed_nodes(self):
        """
        With constant parallel data load(on docs and collections) do:
        0. Pick majority nodes for failover
        1. Induce failure on step0 nodes
        2. Failover failed nodes + a healthy node (more nodes than failed nodes)
        2. Rebalance
        3. Remove failures if you had added them
        4. Add rebalanced out nodes back again
        """
        if self.create_zones:
            self.server_to_fail = self.shuffle_nodes_between_two_zones()
        else:
            self.server_to_fail = self.servers_to_fail()

        self.data_load_flag = True
        self.data_loading_thread = threading.Thread(target=self.data_load)
        self.data_loading_thread.start()

        if self.failover_action:
            self.log.info("Inducing failure {0} on nodes: {1}".
                          format(self.failover_action, self.server_to_fail))
            self.custom_induce_failure()
            self.sleep(60, "Wait before failing over")

        failover_nodes =  [node for node in self.server_to_fail]
        failover_nodes.append(self.nodes_in_cluster[-1]) # healthy node
        _ = self.task.failover(self.nodes_in_cluster, failover_nodes=failover_nodes,
                               graceful=False, wait_for_pending=120,
                               allow_unsafe=True,
                               all_at_once=True)
        self.wait_for_failover_or_assert(len(failover_nodes))

        self.log.info("Rebalancing out nodes {0}".format(self.server_to_fail))
        rebalance_task = self.task.async_rebalance(self.nodes_in_cluster, [], [],
                                                   retry_get_process_num=100)
        self.wait_for_rebalance_to_complete(rebalance_task)
        if self.failover_action:
            self.custom_remove_failure()
            self.sleep(60, "wait after removing failure")

        self.log.info("Adding back nodes which were failed and rebalanced out".
                      format(self.server_to_fail))
        rebalance_task = self.task.async_rebalance(self.nodes_in_cluster, self.server_to_fail, [],
                                                   retry_get_process_num=100)
        self.wait_for_rebalance_to_complete(rebalance_task)

        self.data_load_flag = False
        self.data_loading_thread.join()
        if self.data_load_exception:
            self.log.error("Caught exception from data load thread")
            self.fail(self.data_load_exception)



