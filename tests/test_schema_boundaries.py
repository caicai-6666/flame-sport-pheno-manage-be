import ast
import unittest
from pathlib import Path

from tests.support import configure_test_environment

configure_test_environment()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROUTER_DIRECTORY = PROJECT_ROOT / "app" / "router"
SCHEMA_DIRECTORY = PROJECT_ROOT / "app" / "schemas"


# 解析 Python 文件的抽象语法树，使分层测试不依赖简单字符串匹配。
def parse_python_module(file_path: Path) -> ast.Module:
    return ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))


# 提取模块中的绝对导入路径，供 Schema 反向依赖检查使用。
def collect_imported_modules(module: ast.Module) -> tuple[str, ...]:
    imported_modules: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)
    return tuple(imported_modules)


# 识别直接或模块属性形式的 BaseModel 父类，防止通过不同导入写法绕过目录约束。
def is_base_model_reference(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Name)
        and expression.id == "BaseModel"
    ) or (
        isinstance(expression, ast.Attribute)
        and expression.attr == "BaseModel"
    )


class SchemaBoundaryTestCase(unittest.TestCase):
    # 保证路由文件不再定义 Pydantic BaseModel，维持 HTTP Schema 的唯一归属。
    def test_router_modules_do_not_define_pydantic_models(self) -> None:
        for file_path in ROUTER_DIRECTORY.glob("*.py"):
            with self.subTest(file_path=file_path.name):
                module = parse_python_module(file_path)
                pydantic_base_model_imported = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "pydantic"
                    and any(alias.name == "BaseModel" for alias in node.names)
                    for node in module.body
                )
                self.assertFalse(pydantic_base_model_imported)
                self.assertFalse(
                    any(
                        is_base_model_reference(base)
                        for node in module.body
                        if isinstance(node, ast.ClassDef)
                        for base in node.bases
                    )
                )

    # 保证 Schema 保持纯传输层，禁止反向依赖路由、服务和仓储。
    def test_schema_modules_do_not_depend_on_upper_or_business_layers(
        self,
    ) -> None:
        forbidden_prefixes = (
            "app.router",
            "app.services",
            "app.repositories",
        )
        for file_path in SCHEMA_DIRECTORY.glob("*.py"):
            with self.subTest(file_path=file_path.name):
                imported_modules = collect_imported_modules(
                    parse_python_module(file_path)
                )
                self.assertFalse(
                    any(
                        imported_module.startswith(forbidden_prefixes)
                        for imported_module in imported_modules
                    )
                )


if __name__ == "__main__":
    unittest.main()
