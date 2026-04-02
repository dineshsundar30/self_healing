*** Settings ***
Library    AppiumLibrary

Suite Teardown    Close All Applications

*** Variables ***
${APPIUM_SERVER}    http://127.0.0.1:4723/wd/hub
${PLATFORM_NAME}    Android
${DEVICE_NAME}      Emulator
${APP_PACKAGE}      com.example.app
${APP_ACTIVITY}     .MainActivity
# Imagine this locator is obsolete. The real ID is now 'login_button_v2' or just has text 'Login'
${BROKEN_LOGIN_BUTTON_LOCATOR}    id=login_button_old 

*** Test Cases ***
Self Healing Demonstration
    [Documentation]    This test demonstrates self-healing intercepts a failure and retries.
    
    # 1. Start Appium Session (Adjust to your actual app constraints)
    Open Application    ${APPIUM_SERVER}    platformName=${PLATFORM_NAME}    deviceName=${DEVICE_NAME}    appPackage=${APP_PACKAGE}    appActivity=${APP_ACTIVITY}

    # 2. This keyword fails behind the scenes, gets patched, and successfully executes
    Click Element       ${BROKEN_LOGIN_BUTTON_LOCATOR}
    
    Log    Test Successfully bypassed the broken locator and continued!
