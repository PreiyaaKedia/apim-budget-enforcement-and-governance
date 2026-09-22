using './main.bicep'

param namePrefix = 'dev'
param audience = 'fd4ee8ef-65be-42e5-8702-aa840ef8e22e'
param apimManagedIdentityClientId = '4ad11483-5daa-4066-ac34-770f3d468f54'
param monthlyLimitUsd = '100.00'
param imageTag = 'admin-budget-v3'
param deployContainerApp = true
