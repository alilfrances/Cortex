add_executable(qt_calls_app src/Scanner.cpp src/helpers.cpp)
qt_add_qml_module(qt_calls_app URI QtCalls QML_FILES qml/Main.qml)
