using './main.bicep'

param namePrefix = 'dev'
param audience = 'fd4ee8ef-65be-42e5-8702-aa840ef8e22e'
param apimManagedIdentityClientId = '4ad11483-5daa-4066-ac34-770f3d468f54'
param consoleClientId = '00000000-0000-0000-0000-000000000000'
param consoleClientSecret = readEnvironmentVariable('CONSOLE_CLIENT_SECRET', '')
param consoleAllowedEmailDomains = ['microsoft.com']
param consoleOwnerUsername = 'owner@microsoft.com'
param consoleOwnerPasswordHash = readEnvironmentVariable('CONSOLE_OWNER_PASSWORD_HASH', '')
param consoleOwnerSessionSecret = readEnvironmentVariable('CONSOLE_OWNER_SESSION_SECRET', '')
param applicationInsightsResourceId = '/subscriptions/8cebb108-a4d5-402b-a0c4-f7556126277f/resourceGroups/rg-a2a-dev/providers/Microsoft.Insights/components/dev-multiagent-ai'
param monthlyLimitUsd = '100.00'
param imageTag = 'managed-identity-v1'
param consoleImageTag = 'managed-identity-v1'
param deployContainerApp = true
param deployConsole = true
