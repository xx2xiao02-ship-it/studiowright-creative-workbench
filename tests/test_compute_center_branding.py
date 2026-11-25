"""The user-facing product name is 算力中心."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PRODUCT_FILES = (
    ROOT / 'packaging/ubuntu/snapshot/workbench/index.html',
    ROOT / 'packaging/ubuntu/snapshot/workbench/api-docs.js',
    ROOT / 'packaging/ubuntu/snapshot/flow/console/static/api.html',
    ROOT / 'packaging/windows/client/Desktop.cs',
    ROOT / 'packaging/windows/client/Setup.cs',
    ROOT / 'packaging/windows/client-payload/reuse_existing.py',
    ROOT / 'packaging/ubuntu/snapshot/workbench/app.js',
)


class ComputeCenterBranding(unittest.TestCase):
    def test_visible_product_labels_do_not_use_the_old_name(self):
        for path in PRODUCT_FILES:
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('创作工作台', text, path)
            self.assertNotIn('工作台', text, path)

    def test_main_and_api_surfaces_identify_compute_center(self):
        main = PRODUCT_FILES[0].read_text(encoding='utf-8')
        api = PRODUCT_FILES[2].read_text(encoding='utf-8')
        self.assertIn('<title>算力中心</title>', main)
        self.assertIn('<h1 id="overviewHeading">操作台</h1>', main)
        self.assertIn('<strong id="breadcrumb">操作台</strong>', main)
        self.assertIn('算力中心 API 调用说明', PRODUCT_FILES[1].read_text(encoding='utf-8'))
        self.assertIn('算力中心', api)


if __name__ == '__main__':
    unittest.main()
