import logging
import time
import datetime
from collections import defaultdict

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from milvus.grpc_gen import milvus_pb2, milvus_pb2_grpc, status_pb2
from milvus.grpc_gen.milvus_pb2 import TopKQueryResult
from milvus.client.abstract import Range
from milvus.client import types as Types

from mishards import (db, settings, exceptions)
from mishards.grpc_utils import mark_grpc_method
from mishards.grpc_utils.grpc_args_parser import GrpcArgsParser as Parser
from mishards import utilities

logger = logging.getLogger(__name__)


class ServiceHandler(milvus_pb2_grpc.MilvusServiceServicer):
    MAX_NPROBE = 2048
    MAX_TOPK = 2048

    def __init__(self, tracer, router, max_workers=multiprocessing.cpu_count(), **kwargs):
        self.table_meta = {}
        self.error_handlers = {}
        self.tracer = tracer
        self.router = router
        self.max_workers = max_workers

    def _do_merge(self, files_n_topk_results, topk, reverse=False, **kwargs):
        status = status_pb2.Status(error_code=status_pb2.SUCCESS,
                                   reason="Success")
        if not files_n_topk_results:
            return status, []

        request_results = defaultdict(list)

        calc_time = time.time()
        for files_collection in files_n_topk_results:
            if isinstance(files_collection, tuple):
                status, _ = files_collection
                return status, []
            for request_pos, each_request_results in enumerate(
                    files_collection.topk_query_result):
                request_results[request_pos].extend(
                    each_request_results.query_result_arrays)
                request_results[request_pos] = sorted(
                    request_results[request_pos],
                    key=lambda x: x.distance,
                    reverse=reverse)[:topk]

        calc_time = time.time() - calc_time
        logger.info('Merge takes {}'.format(calc_time))

        results = sorted(request_results.items())
        topk_query_result = []

        for result in results:
            query_result = TopKQueryResult(query_result_arrays=result[1])
            topk_query_result.append(query_result)

        return status, topk_query_result

    def _do_query(self,
                  context,
                  table_id,
                  table_meta,
                  vectors,
                  topk,
                  nprobe,
                  range_array=None,
                  **kwargs):
        metadata = kwargs.get('metadata', None)
        range_array = [
            utilities.range_to_date(r, metadata=metadata) for r in range_array
        ] if range_array else None

        routing = {}
        p_span = None if self.tracer.empty else context.get_active_span(
        ).context
        with self.tracer.start_span('get_routing', child_of=p_span):
            routing = self.router.routing(table_id,
                                          range_array=range_array,
                                          metadata=metadata)
        logger.info('Routing: {}'.format(routing))

        metadata = kwargs.get('metadata', None)

        rs = []
        all_topk_results = []

        def search(addr, query_params, vectors, topk, nprobe, **kwargs):
            logger.info(
                'Send Search Request: addr={};params={};nq={};topk={};nprobe={}'
                .format(addr, query_params, len(vectors), topk, nprobe))

            conn = self.router.query_conn(addr, metadata=metadata)
            start = time.time()
            span = kwargs.get('span', None)
            span = span if span else (None if self.tracer.empty else
                                      context.get_active_span().context)

            with self.tracer.start_span('search_{}'.format(addr),
                                        child_of=span):
                ret = conn.search_vectors_in_files(
                    table_name=query_params['table_id'],
                    file_ids=query_params['file_ids'],
                    query_records=vectors,
                    top_k=topk,
                    nprobe=nprobe,
                    lazy_=True)
                end = time.time()
                logger.info('search_vectors_in_files takes: {}'.format(end - start))

                all_topk_results.append(ret)

        with self.tracer.start_span('do_search', child_of=p_span) as span:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                for addr, params in routing.items():
                    res = pool.submit(search,
                                      addr,
                                      params,
                                      vectors,
                                      topk,
                                      nprobe,
                                      span=span)
                    rs.append(res)

                for res in rs:
                    res.result()

        reverse = table_meta.metric_type == Types.MetricType.IP
        with self.tracer.start_span('do_merge', child_of=p_span):
            return self._do_merge(all_topk_results,
                                  topk,
                                  reverse=reverse,
                                  metadata=metadata)

    def _create_table(self, table_schema):
        return self.router.connection().create_table(table_schema)

    @mark_grpc_method
    def CreateTable(self, request, context):
        _status, _table_schema = Parser.parse_proto_TableSchema(request)

        if not _status.OK():
            return status_pb2.Status(error_code=_status.code,
                                     reason=_status.message)

        logger.info('CreateTable {}'.format(_table_schema['table_name']))

        _status = self._create_table(_table_schema)

        return status_pb2.Status(error_code=_status.code,
                                 reason=_status.message)

    def _has_table(self, table_name, metadata=None):
        return self.router.connection(metadata=metadata).has_table(table_name)

    @mark_grpc_method
    def HasTable(self, request, context):
        _status, _table_name = Parser.parse_proto_TableName(request)

        if not _status.OK():
            return milvus_pb2.BoolReply(status=status_pb2.Status(
                error_code=_status.code, reason=_status.message),
                bool_reply=False)

        logger.info('HasTable {}'.format(_table_name))

        _status, _bool = self._has_table(_table_name,
                                metadata={'resp_class': milvus_pb2.BoolReply})

        return milvus_pb2.BoolReply(status=status_pb2.Status(
            error_code=_status.code, reason=_status.message),
            bool_reply=_bool)

    def _delete_table(self, table_name):
        return self.router.connection().delete_table(table_name)

    @mark_grpc_method
    def DropTable(self, request, context):
        _status, _table_name = Parser.parse_proto_TableName(request)

        if not _status.OK():
            return status_pb2.Status(error_code=_status.code,
                                     reason=_status.message)

        logger.info('DropTable {}'.format(_table_name))

        _status = self._delete_table(_table_name)

        return status_pb2.Status(error_code=_status.code,
                                 reason=_status.message)

    def _create_index(self, table_name, index):
        return self.router.connection().create_index(table_name, index)

    @mark_grpc_method
    def CreateIndex(self, request, context):
        _status, unpacks = Parser.parse_proto_IndexParam(request)

        if not _status.OK():
            return status_pb2.Status(error_code=_status.code,
                                     reason=_status.message)

        _table_name, _index = unpacks

        logger.info('CreateIndex {}'.format(_table_name))

        # TODO: interface create_table incompleted
        _status = self._create_index(_table_name, _index)

        return status_pb2.Status(error_code=_status.code,
                                 reason=_status.message)

    def _add_vectors(self, param, metadata=None):
        return self.router.connection(metadata=metadata).add_vectors(
            None, None, insert_param=param)

    @mark_grpc_method
    def Insert(self, request, context):
        logger.info('Insert')
        # TODO: Ths SDK interface add_vectors() could update, add a key 'row_id_array'
        _status, _ids = self._add_vectors(
            metadata={'resp_class': milvus_pb2.VectorIds}, param=request)
        return milvus_pb2.VectorIds(status=status_pb2.Status(
            error_code=_status.code, reason=_status.message),
            vector_id_array=_ids)

    @mark_grpc_method
    def Search(self, request, context):

        table_name = request.table_name

        topk = request.topk
        nprobe = request.nprobe

        logger.info('Search {}: topk={} nprobe={}'.format(
            table_name, topk, nprobe))

        metadata = {'resp_class': milvus_pb2.TopKQueryResultList}

        if nprobe > self.MAX_NPROBE or nprobe <= 0:
            raise exceptions.InvalidArgumentError(
                message='Invalid nprobe: {}'.format(nprobe), metadata=metadata)

        if topk > self.MAX_TOPK or topk <= 0:
            raise exceptions.InvalidTopKError(
                message='Invalid topk: {}'.format(topk), metadata=metadata)

        table_meta = self.table_meta.get(table_name, None)

        if not table_meta:
            status, info = self.router.connection(
                metadata=metadata).describe_table(table_name)
            if not status.OK():
                raise exceptions.TableNotFoundError(table_name,
                                                    metadata=metadata)

            self.table_meta[table_name] = info
            table_meta = info

        start = time.time()

        query_record_array = []

        for query_record in request.query_record_array:
            query_record_array.append(list(query_record.vector_data))

        query_range_array = []
        for query_range in request.query_range_array:
            query_range_array.append(
                Range(query_range.start_value, query_range.end_value))

        status, results = self._do_query(context,
                                         table_name,
                                         table_meta,
                                         query_record_array,
                                         topk,
                                         nprobe,
                                         query_range_array,
                                         metadata=metadata)

        now = time.time()
        logger.info('SearchVector takes: {}'.format(now - start))

        topk_result_list = milvus_pb2.TopKQueryResultList(
            status=status_pb2.Status(error_code=status.error_code,
                                     reason=status.reason),
            topk_query_result=results)
        return topk_result_list

    @mark_grpc_method
    def SearchInFiles(self, request, context):
        raise NotImplemented()

    def _describe_table(self, table_name, metadata=None):
        return self.router.connection(metadata=metadata).describe_table(table_name)

    @mark_grpc_method
    def DescribeTable(self, request, context):
        _status, _table_name = Parser.parse_proto_TableName(request)

        if not _status.OK():
            return milvus_pb2.TableSchema(status=status_pb2.Status(
                error_code=_status.code, reason=_status.message), )

        metadata = {'resp_class': milvus_pb2.TableSchema}

        logger.info('DescribeTable {}'.format(_table_name))
        _status, _table = self._describe_table(metadata=metadata,
                                               table_name=_table_name)

        if _status.OK():
            return milvus_pb2.TableSchema(
                table_name=_table_name,
                index_file_size=_table.index_file_size,
                dimension=_table.dimension,
                metric_type=_table.metric_type,
                status=status_pb2.Status(error_code=_status.code,
                                         reason=_status.message),
            )

        return milvus_pb2.TableSchema(
            table_name=_table_name,
            status=status_pb2.Status(error_code=_status.code,
                                     reason=_status.message),
        )

    def _count_table(self, table_name, metadata=None):
        return self.router.connection(
            metadata=metadata).get_table_row_count(table_name)

    @mark_grpc_method
    def CountTable(self, request, context):
        _status, _table_name = Parser.parse_proto_TableName(request)

        if not _status.OK():
            status = status_pb2.Status(error_code=_status.code,
                                       reason=_status.message)

            return milvus_pb2.TableRowCount(status=status)

        logger.info('CountTable {}'.format(_table_name))

        metadata = {'resp_class': milvus_pb2.TableRowCount}
        _status, _count = self._count_table(_table_name, metadata=metadata)

        return milvus_pb2.TableRowCount(
            status=status_pb2.Status(error_code=_status.code,
                                     reason=_status.message),
            table_row_count=_count if isinstance(_count, int) else -1)

    def _get_server_version(self, metadata=None):
        return self.router.connection(metadata=metadata).server_version()

    @mark_grpc_method
    def Cmd(self, request, context):
        _status, _cmd = Parser.parse_proto_Command(request)
        logger.info('Cmd: {}'.format(_cmd))

        if not _status.OK():
            return milvus_pb2.StringReply(status=status_pb2.Status(
                error_code=_status.code, reason=_status.message))

        metadata = {'resp_class': milvus_pb2.StringReply}

        if _cmd == 'version':
            _status, _reply = self._get_server_version(metadata=metadata)
        else:
            _status, _reply = self.router.connection(
                metadata=metadata).server_status()

        return milvus_pb2.StringReply(status=status_pb2.Status(
            error_code=_status.code, reason=_status.message),
            string_reply=_reply)

    def _show_tables(self, metadata=None):
        return self.router.connection(metadata=metadata).show_tables()

    @mark_grpc_method
    def ShowTables(self, request, context):
        logger.info('ShowTables')
        metadata = {'resp_class': milvus_pb2.TableName}
        _status, _results = self._show_tables(metadata=metadata)

        return milvus_pb2.TableNameList(status=status_pb2.Status(
            error_code=_status.code, reason=_status.message),
            table_names=_results)

    def _delete_by_range(self, table_name, start_date, end_date):
        return self.router.connection().delete_vectors_by_range(table_name,
                                                                start_date,
                                                                end_date)

    @mark_grpc_method
    def DeleteByRange(self, request, context):
        _status, unpacks = \
            Parser.parse_proto_DeleteByRangeParam(request)

        if not _status.OK():
            return status_pb2.Status(error_code=_status.code,
                                     reason=_status.message)

        _table_name, _start_date, _end_date = unpacks

        logger.info('DeleteByRange {}: {} {}'.format(_table_name, _start_date,
                                                     _end_date))
        _status = self._delete_by_range(_table_name, _start_date, _end_date)
        return status_pb2.Status(error_code=_status.code,
                                 reason=_status.message)

    def _preload_table(self, table_name):
        return self.router.connection().preload_table(table_name)

    @mark_grpc_method
    def PreloadTable(self, request, context):
        _status, _table_name = Parser.parse_proto_TableName(request)

        if not _status.OK():
            return status_pb2.Status(error_code=_status.code,
                                     reason=_status.message)

        logger.info('PreloadTable {}'.format(_table_name))
        _status = self._preload_table(_table_name)
        return status_pb2.Status(error_code=_status.code,
                                 reason=_status.message)

    def _describe_index(self, table_name, metadata=None):
        return self.router.connection(metadata=metadata).describe_index(table_name)

    @mark_grpc_method
    def DescribeIndex(self, request, context):
        _status, _table_name = Parser.parse_proto_TableName(request)

        if not _status.OK():
            return milvus_pb2.IndexParam(status=status_pb2.Status(
                error_code=_status.code, reason=_status.message))

        metadata = {'resp_class': milvus_pb2.IndexParam}

        logger.info('DescribeIndex {}'.format(_table_name))
        _status, _index_param = self._describe_index(table_name=_table_name,
                                                     metadata=metadata)

        if not _index_param:
            return milvus_pb2.IndexParam(status=status_pb2.Status(
                error_code=_status.code, reason=_status.message))

        _index = milvus_pb2.Index(index_type=_index_param._index_type,
                                  nlist=_index_param._nlist)

        return milvus_pb2.IndexParam(status=status_pb2.Status(
            error_code=_status.code, reason=_status.message),
            table_name=_table_name,
            index=_index)

    def _drop_index(self, table_name):
        return self.router.connection().drop_index(table_name)

    @mark_grpc_method
    def DropIndex(self, request, context):
        _status, _table_name = Parser.parse_proto_TableName(request)

        if not _status.OK():
            return status_pb2.Status(error_code=_status.code,
                                     reason=_status.message)

        logger.info('DropIndex {}'.format(_table_name))
        _status = self._drop_index(_table_name)
        return status_pb2.Status(error_code=_status.code,
                                 reason=_status.message)
