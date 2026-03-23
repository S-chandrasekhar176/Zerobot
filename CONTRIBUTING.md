# Contributing to ZeroBot

Thank you for your interest in contributing to ZeroBot! We welcome bug reports, feature requests, documentation improvements, and code contributions.

---

## 📋 Code of Conduct

- Be respectful and inclusive
- Disagreements are OK; personal attacks are not
- No harassment of any kind

---

## 🐛 Reporting Bugs

1. **Check existing issues** to avoid duplicates
2. **Click "Issues" → "New Issue"** and select **Bug Report**
3. Include:
   - Python version: `python --version`
   - Mode (paper/s_mode/live) where the bug occurred
   - Steps to reproduce
   - Expected vs. actual behavior
   - Relevant logs from `logs/errors/`
   - Environment: config/settings.yaml (with sensitive data removed)

Example:
```
Title: Paper broker crashes when symbol not found

Steps:
1. Add "INVALID_SYMBOL" to settings.yaml
2. Run: python main.py --mode paper
3. Wait 5 seconds

Error:
KeyError: 'INVALID_SYMBOL' in broker/paper_broker.py line 145

Environment:
- Python 3.10
- Mode: paper
- Broker: paper
```

---

## 💡 Feature Requests

1. **Click "Issues" → "Discussions"** for initial feedback
2. **Describe the use case**: Why do you need this feature?
3. **Suggest implementation**: High-level architecture if possible
4. **Link related issues** if there are any

Example:
```
Title: Support for intraday options trading

Use case: Generate theta decay signals for short strangles

Implementation idea:
- Add NSE option chain real-time streaming
- Calculate Greeks (delta/gamma/theta at each tick)
- Generate signals based on theta targets
```

---

## 🔧 Development Setup

### Fork and Clone

```bash
git clone https://github.com/YOUR_USERNAME/zerobot.git
cd zerobot
git remote add upstream https://github.com/UPSTREAM_REPO/zerobot.git
```

### Set Up Environment

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt
pip install -r requirements-dev.txt  # dev dependencies (pytest, black, etc.)
```

### Run Tests Before Development

```bash
python test_system_boot.py
python healthcheck.py
```

---

## 🎯 Contributing Code

### 1. Create a Feature Branch

```bash
git checkout -b feature/my-feature
# or
git checkout -b fix/my-bug
```

Branch naming:
- `feature/description-of-feature` for new features
- `fix/description-of-bug` for bug fixes
- `docs/description` for documentation
- `refactor/description` for refactoring

### 2. Code Style Guidelines

**Python:**
- Follow PEP 8
- Use type hints for function signatures
- Max line length: 100 characters
- Use meaningful variable names (avoid `x`, `y`, `tmp`)

**Example:**
```python
def calculate_kelly_size(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    capital: float
) -> float:
    """
    Calculate position size using Kelly criterion.
    
    Args:
        win_rate: Probability of winning (0-1)
        avg_win: Average profit on winning trades
        avg_loss: Average loss on losing trades
        capital: Available capital
    
    Returns:
        Recommended position size in capital units
    """
    if avg_loss == 0:
        return 0
    
    kelly_pct = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_loss
    kelly_fraction = max(0, min(kelly_pct, 0.25))  # Cap at 25%
    
    return kelly_fraction * capital
```

### 3. Add/Update Docstrings

Every function and class must have a docstring:

```python
class MomentumStrategy(BaseStrategy):
    """
    Momentum-based trading strategy.
    
    Generates BUY signals when price breaks above rolling high,
    and SELL signals when price breaks below rolling low.
    Suitable for trending markets (VIX < 25).
    """
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> SignalDict:
        """
        Generate momentum signal.
        
        Args:
            ohlcv: DataFrame with columns [open, high, low, close, volume]
        
        Returns:
            SignalDict with keys: symbol, side, confidence, reason
        """
```

### 4. Commit Messages

Use clear, descriptive commit messages:

```
feat: Add options Greeks calculation for iv_rank strategy

- Implement Black-Scholes Greeks calculator
- Add NSE option token mapping
- Update risk engine with options vega gate

Fixes #42
```

Format:
```
<type>: <subject>

<body>

<footer>
```

Types:
- `feat:` — New feature
- `fix:` — Bug fix
- `refactor:` — Code refactoring (no behavior change)
- `docs:` — Documentation update
- `test:` — Test additions/improvements
- `chore:` — Build, dependency, or tool changes

### 5. Add Tests

All features must have tests:

```python
# tests/test_my_feature.py
import pytest
from strategies.my_strategy import MyStrategy
from core.state_manager import state_mgr

def test_my_feature_generates_signal():
    """Test that MyStrategy generates valid BUY signal."""
    strategy = MyStrategy()
    
    ohlcv = pd.DataFrame({
        'close': [100, 102, 104, 106, 108],
        'volume': [1000, 1100, 1200, 1300, 1400]
    })
    
    signal = strategy.generate_signal(ohlcv)
    
    assert signal['side'] == 'BUY'
    assert 0 <= signal['confidence'] <= 100

def test_my_feature_passes_risk_gates():
    """Test that signals pass risk validation."""
    from risk.risk_engine import RiskEngine
    
    # Setup
    engine = RiskEngine()
    signal = {'symbol': 'RELIANCE', 'side': 'BUY', 'qty': 10}
    
    # Evaluate
    result = engine.evaluate_gates(signal)
    
    # Assert
    assert result.passed == True
```

Run tests:
```bash
pytest tests/ -v
```

### 6. Document Your Changes

Update relevant docs:

```bash
# If adding a new strategy:
- Update docs/architecture.md (add to strategies section)
- Update docs/usage.md (add configuration example)
- Update README.md (add to features/roadmap)
```

---

## 📝 Adding a New Strategy

### File Structure

```python
# strategies/my_strategy.py
from strategies.base_strategy import BaseStrategy
from typing import Dict
import pandas as pd

class MyStrategy(BaseStrategy):
    """
    Description of what this strategy does.
    """
    
    def __init__(self):
        super().__init__()
        self.lookback = 20      # Add configurable parameters
        self.threshold = 0.02
    
    def generate_signal(
        self,
        ohlcv: pd.DataFrame,
        state: 'BotState'
    ) -> Dict:
        """
        Generate trading signal.
        
        Returns:
            {
                'symbol': str,
                'side': 'BUY' | 'SELL' | None,
                'confidence': float (0-100),
                'reason': str,
                'qty': int,
                'order_type': 'LIMIT' | 'MARKET'
            }
        """
        if len(ohlcv) < self.lookback:
            return {'side': None, 'reason': 'Insufficient data'}
        
        # Your signal logic here
        close_prices = ohlcv['close'].tail(self.lookback)
        recent_close = close_prices.iloc[-1]
        
        signal = {
            'side': 'BUY' if recent_close > close_prices.mean() else None,
            'confidence': 75,
            'reason': 'Price above 20-period MA',
            'qty': 5,
            'order_type': 'LIMIT'
        }
        
        return signal
```

### Register in settings.yaml

```yaml
strategies:
  my_strategy:
    enabled: true
    weight: 0.5
    params:
      lookback: 20
      threshold: 0.02
```

### Add Test

```python
# tests/test_my_strategy.py
from strategies.my_strategy import MyStrategy

def test_my_strategy_bullish():
    strategy = MyStrategy()
    ohlcv = pd.DataFrame({
        'close': list(range(95, 115))  # Rising trend
    })
    signal = strategy.generate_signal(ohlcv)
    assert signal['side'] == 'BUY'
```

---

## 🔌 Adding a New Broker

### File Structure

```python
# broker/my_broker.py
from broker.base_broker import BaseBroker

class MyBroker(BaseBroker):
    """Integrate MyBrokerAPI."""
    
    def __init__(self, api_key: str, account_id: str):
        self.api_key = api_key
        self.account_id = account_id
    
    def connect(self) -> bool:
        """Authenticate and connect to broker."""
        # Your connection logic
        return True
    
    def place_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str,
        price: float = None
    ) -> Dict:
        """
        Place order on broker.
        
        Returns:
            {
                'id': str,
                'symbol': str,
                'qty': int,
                'filled': int,
                'status': 'OPEN' | 'FILLED' | 'REJECTED',
                'entry_price': float
            }
        """
        # Implementation
        pass
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel open order."""
        pass
    
    def get_positions(self) -> Dict[str, Dict]:
        """Get all open positions."""
        pass
    
    def get_quote(self, symbol: str) -> Dict:
        """Get current price for symbol."""
        pass
```

### Register in factory

```python
# broker/factory.py
def create_broker(config):
    if config.broker.name == "my_broker":
        return MyBroker(config.api_key, config.account_id)
```

---

## 🔄 Pull Request Process

1. **Keep changes focused**: One feature/fix per PR
2. **Rebase before submitting**:
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

3. **Push to your fork**:
   ```bash
   git push origin feature/my-feature
   ```

4. **Open PR on GitHub**:
   - Clear title: `feat: Add X`, `fix: Resolve Y`
   - Link related issues: `Closes #42`, `Related to #99`
   - Describe what changed and why
   - Run tests locally first: `pytest tests/ -v`

5. **Respond to feedback**:
   - Address reviewer comments
   - Push updates to same branch (don't open new PR)

6. **Merge after approval**:
   - Maintainers will merge when ready

Example PR description:
```markdown
## Description
Adds support for volatility-adjusted position sizing using IV percentile.

## Changes
- ModifyKellySizer to use current IV/IV_SMA ratio
- Add IVRankCalculator to data/feeds/
- Update risk_engine.py to pass IV data

## Testing
- Unit tests: `test_iv_rank_sizer.py`
- Manual: Paper trading with high volatility day confirmed sizing reduction

## Related Issues
Closes #85
```

---

## 📈 Improving Documentation

Documentation lives in:
- `README.md` — Project overview
- `docs/architecture.md` — System design
- `docs/usage.md` — Setup and examples
- `CONTRIBUTING.md` — This file

To improve docs:
1. Edit the relevant markdown file
2. Test formatting (headings, code blocks, links)
3. Submit PR with title: `docs: Clarify X` or `docs: Add X guide`

---

## 🏆 Tips for Successful Contributions

✅ **Good PR**:
- Solves one specific problem
- Has clear commit message
- Includes tests
- Doesn't break existing tests
- Updates relevant docs
- Small (< 400 lines)

❌ **Avoid**:
- Large PRs (split into smaller ones)
- Unrelated changes in one PR
- No tests
- API keys or secrets in code
- Reformatting unrelated code

---

## ❓ Questions?

- **GitHub Discussions**: Ask questions in Discussions (tagged `question`)
- **GitHub Issues**: Report bugs or request features
- **Email**: Contact maintainers for security issues (don't open public issue)

---

## 🙏 Thank You!

Every contribution — code, docs, bug reports, ideas — makes ZeroBot better.

**Happy trading! 🚀**
