# What the UI checks measure with

Not a command. `fx` takes from this folder what a UI check needs to measure a screen against its design:

- `runtime/uicheck/`: the comparer of a screen with its design package (web pages and app screens);
- `runtime/app.js`: how a UI check drives a mobile app on a virtual phone (Appium);
- `txlib/design.py` (with `txlib/core.py`): the reading of a design package into its state table.
