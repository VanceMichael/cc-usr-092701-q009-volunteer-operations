# 国际赛事志愿服务排班

本项目保存国际赛事志愿服务排班所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

赛事服务中心、高校管理员、志愿者、组委会调度员

## 事实资料

- 第48届世界技能大赛志愿者承担赛事服务、酒店、制证和涉外沟通
- 志愿者团队从清晨到深夜持续提供保障
- 小语种志愿者负责外籍采访团、机场抵离和文化沟通

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行检查

```bash
python3 -m src.volunteer_operations.context fixtures/context.json
```
