from __future__ import annotations

from UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE import EnterpriseApp
from runtime_profiles import apply_profile


if __name__ == "__main__":
    apply_profile()
    app = EnterpriseApp()
    app.mainloop()
