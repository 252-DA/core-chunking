import sys

from . import chunking_pb2 as chunking_pb2

sys.modules.setdefault("chunking_pb2", chunking_pb2)

from . import chunking_pb2_grpc as chunking_pb2_grpc

__all__ = ["chunking_pb2", "chunking_pb2_grpc"]
