# quark-follow
自用的夸克自动更新转存工具
依赖python3.纯shell执行.自身无循环函数.
依赖openlist.

#结构介绍
1.9版.已完成部分：
seedhub解析式功能
添加缺集功能
多任务串行功能
数据库缓存功能
自动数据库维护
配置单独分离功能

#使用方法
1.启动并调用 playwright-python:chromium容器.
  目标:解析器工作的必须依赖。
  执行脚本 seedhub_start.sh检测并运行容器.
2.编辑临时工作布置文件 resources.json .该文件中有
  示例且便于拓展
3.编辑 config 文件填写必要变量.
4.执行 resource_check.sh 主入口开始工作.

##该脚本不存在自动循环函数.需第三方工具执行循环操作
##
