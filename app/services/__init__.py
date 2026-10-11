"""业务服务层。

这一层只关心"做什么"，不关心 HTTP、不关心数据库 session 从哪来。
这样 CLI、Web API、后台 Worker 可以共用同一份实现。
"""
