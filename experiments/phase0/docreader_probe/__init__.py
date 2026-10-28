"""P0-T10 DocReader wire 探针（实验代码，不是产品实现）。

目录内容
--------
``proto/docreader.proto``
    WeKnora ``docreader/proto/docreader.proto`` 的**逐字节副本**
    （sha256 ``33d4135cfcd2ec95f8d160d6cefdd54be669167e8b8fb69ef81095d218a738d0``，
    submodule ``662abaf9f944e37689d04c42160034b2cdec6829``），只为生成 stub 与证据核验。
``generated/``
    ``grpc_tools.protoc`` 从上述 proto 生成的 ``docreader_pb2`` /
    ``docreader_pb2_grpc``；工具链版本见 ``generated/PROVENANCE.json``。
``server.py``
    **按参考实现 wire 语义复刻的 shim**（帧序、``error`` 字段、status 行为、
    取消/超时观察点），不是真实 DocReader 服务。
``client.py``
    PyKnora 侧最小客户端 + 流完整性判定（meta 唯一/首帧、``error``、EOF、图片数）。

边界
----
真实 DocReader 镜像因 digest/许可 ``UNKNOWN``（T09 §4.1/§4.3/§9）**未运行**，
本目录不能作为"真实服务已验证"的证据；对应验收项记 ``NOT_RUN``。
"""
